"""Mangarr 1.3 integrated metadata lifecycle acceptance coverage.

The scenarios in this module deliberately cross route, metadata, provenance,
grab, import, database, and filesystem boundaries.  Only remote provider,
indexer, and download-client I/O is replaced with deterministic doubles.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict
from unittest.mock import AsyncMock

import httpx
import pytest


class LifecycleEnv(TypedDict):
    db_path: Path
    library_root: Path
    completed_root: Path
    api_key: str


class ProviderDoubles(TypedDict):
    search: AsyncMock
    by_id: AsyncMock
    mu_search: AsyncMock
    mangadex_id: AsyncMock
    chapter_map: AsyncMock
    cover_download: AsyncMock


class _MangaDexFeedResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"data": [], "total": 0}


class _MangaDexFeedClient:
    """Network-boundary double for an empty, successful MangaDex feed."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def __aenter__(self) -> _MangaDexFeedClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        del args
        return False

    async def get(self, *args: object, **kwargs: object) -> _MangaDexFeedResponse:
        del args, kwargs
        return _MangaDexFeedResponse()


@pytest.fixture
def lifecycle_env(tmp_path: Path) -> Iterator[LifecycleEnv]:
    import main
    import security
    import shared

    db_path = tmp_path / "metadata-milestone.db"
    library_root = tmp_path / "library"
    completed_root = tmp_path / "completed"
    key_dir = tmp_path / "keys"
    library_root.mkdir()
    completed_root.mkdir()

    original_main_db = main.DB_PATH
    original_shared_db = shared.DB_PATH
    original_cipher = security._SECRET_CIPHER
    original_main_config = dict(main.CONFIG)
    original_shared_config = dict(shared.CONFIG)

    main.DB_PATH = str(db_path)
    shared.DB_PATH = str(db_path)
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(str(key_dir))
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM root_folders")
        db.execute(
            "INSERT INTO root_folders(id,path,label,is_default)"
            " VALUES(1,?,'Acceptance library',1)",
            (str(library_root),),
        )
        db.execute("DELETE FROM indexers")
        db.execute(
            "INSERT INTO indexers(id,name,type,url,enabled,categories)"
            " VALUES(31,'Acceptance indexer','torznab',"
            " 'https://indexer.invalid',1,'[7000]')"
        )
        db.execute("DELETE FROM download_clients")
        db.execute(
            "INSERT INTO download_clients(id,name,type,host,enabled)"
            " VALUES(77,'Acceptance qBit','qbittorrent',"
            " 'https://download.invalid',1)"
        )
        encrypted_key = db.execute(
            "SELECT value FROM settings WHERE key='api_key'"
        ).fetchone()[0]

    try:
        yield {
            "db_path": db_path,
            "library_root": library_root,
            "completed_root": completed_root,
            "api_key": security.decrypt_secret(encrypted_key),
        }
    finally:
        main.DB_PATH = original_main_db
        shared.DB_PATH = original_shared_db
        security._SECRET_CIPHER = original_cipher
        main.CONFIG.clear()
        main.CONFIG.update(original_main_config)
        shared.CONFIG.clear()
        shared.CONFIG.update(original_shared_config)


def _anilist_record(**overrides: object) -> dict[str, Any]:
    record: dict[str, Any] = {
        "anilist_id": 707,
        "mal_id": 1707,
        "title": "Alpha Beta Gamma Delta Epsilon",
        "romaji_title": "Different Romaji Name",
        "aliases": ["Acceptance Alternate"],
        "genres": ["Action", "Drama"],
        "cover_url": "https://covers.invalid/acceptance.jpg",
        "status": "FINISHED",
        "format": "MANGA",
        "volumes": 4,
        "chapters": 40,
        "pub_year": 2021,
        "description": "Initial provider description",
        "source": "anilist",
    }
    record.update(overrides)
    return record


def _chapter_map(chapter_count: int, volume_count: int) -> dict[str, int]:
    chapters_per_volume = math.ceil(chapter_count / volume_count)
    return {
        str(chapter): min(volume_count, math.ceil(chapter / chapters_per_volume))
        for chapter in range(1, chapter_count + 1)
    }


def _install_provider_doubles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    search_results: list[dict[str, Any]],
    exact_record: dict[str, Any],
    chapter_map: dict[str, int] | None,
    mu_results: list[dict[str, Any]] | None = None,
) -> ProviderDoubles:
    import cover_images
    import metadata_enrichment
    import metadata_service
    from routers import mangadex_ as mangadex_router

    search = AsyncMock(return_value=search_results)
    by_id = AsyncMock(return_value=exact_record)
    mu_search = AsyncMock(return_value=mu_results or [])

    async def resolve_mangadex_id(
        title: str,
        anilist_id: int | None,
        mu_id: str | None,
    ) -> tuple[str | None, dict[str, str]]:
        del title, mu_id
        return (f"mdx-{anilist_id}", {}) if anilist_id else (None, {})

    mangadex_id = AsyncMock(side_effect=resolve_mangadex_id)
    map_fetch = AsyncMock(return_value=chapter_map or {})
    cover_download = AsyncMock(
        return_value={"ok": True, "status": "downloaded", "bytes": 128}
    )

    monkeypatch.setattr(metadata_service, "anilist_search", search)
    monkeypatch.setattr(metadata_service, "fetch_anilist_by_id", by_id)
    monkeypatch.setattr(metadata_service, "download_cover", cover_download)
    monkeypatch.setattr(cover_images, "download_cover", cover_download)
    monkeypatch.setattr(metadata_enrichment, "mu_search", mu_search)
    monkeypatch.setattr(metadata_enrichment, "fetch_mangadex_id", mangadex_id)
    monkeypatch.setattr(
        metadata_enrichment,
        "fetch_chapter_volume_map",
        map_fetch,
    )
    # Replace only the router's module reference. Patching AsyncClient on the
    # shared ``httpx`` module would also replace this test's ASGI client.
    monkeypatch.setattr(
        mangadex_router,
        "httpx",
        SimpleNamespace(AsyncClient=_MangaDexFeedClient),
    )
    return {
        "search": search,
        "by_id": by_id,
        "mu_search": mu_search,
        "mangadex_id": mangadex_id,
        "chapter_map": map_fetch,
        "cover_download": cover_download,
    }


def _field_state(series_id: int, field_name: str) -> dict[str, Any]:
    from metadata_provenance import get_metadata_field_states

    return next(
        field
        for field in get_metadata_field_states(series_id)
        if field["field_name"] == field_name
    )


def _make_cbz(path: Path, marker: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", marker)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _client(app: object) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://mangarr.test",
        follow_redirects=False,
    ) as client:
        yield client


async def _wait_for_background_task(name: str) -> None:
    """Join a production task spawned by a creation route on this event loop."""
    import tasks

    matches = [task for task in tasks._BACKGROUND_TASKS if task.get_name() == name]
    if matches:
        await asyncio.gather(*matches)


def test_scenario_a_new_series_crosses_metadata_grab_import_and_rescan(
    lifecycle_env: LifecycleEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider/API title mix-up or lifecycle state reset must fail this test."""
    import grab_core
    import main
    import metadata_service
    from clients import GrabResult
    from import_pipeline import _process_auto_import, _queue_import
    from metadata_provenance import record_metadata_selections
    from routers import indexers
    from shared import get_db

    discovery_title = "Alpha Beta Gamma Delta Epsilon Zeta"
    fuzzy_record = _anilist_record()
    exact_record = _anilist_record(
        title="Provider Exact Title",
        description="Description refreshed by exact identity",
    )
    providers = _install_provider_doubles(
        monkeypatch,
        search_results=[fuzzy_record],
        exact_record=exact_record,
        chapter_map=_chapter_map(40, 4),
        mu_results=[
            {
                "title": discovery_title,
                "mu_id": "foreign-same-title",
                "volumes": 99,
            },
            {"title": discovery_title, "mu_id": "mu-707", "volumes": 4},
        ],
    )

    async def scenario() -> None:
        csrf_token = "csrf-milestone-a-" + "a" * 32
        api_headers = {"X-Api-Key": lifecycle_env["api_key"]}
        csrf = {"X-CSRFToken": csrf_token}

        async for client in _client(main.app):
            client.cookies.set("csrftoken", csrf_token)
            created = await client.post(
                "/api/v1/series",
                json={
                    "title": discovery_title,
                    "searchPattern": discovery_title,
                    "mangaUpdatesId": "mu-707",
                    "totalVolumes": 1,
                    "rootFolderId": 1,
                    "monitored": True,
                },
                headers=api_headers,
            )
            assert created.status_code == 200, created.text
            series_id = int(created.json()["series"]["id"])

            initial_title = _field_state(series_id, "title")
            assert initial_title["value"] == discovery_title
            assert initial_title["selected_source"] == "api"
            assert initial_title["locked"] is False

            await _wait_for_background_task(f"api_series:{series_id}:metadata")

            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                db.row_factory = sqlite3.Row
                first = db.execute(
                    "SELECT anilist_id,mal_id,mu_id,description,cover_url,status,"
                    " pub_year,total_volumes,total_chapters,vol_count_source,"
                    " chapter_count_source,metadata_status,last_metadata_refresh"
                    " FROM series WHERE id=?",
                    (series_id,),
                ).fetchone()
                confidence = db.execute(
                    "SELECT confidence FROM series_metadata_candidates"
                    " WHERE series_id=? AND field_name='title' AND source='anilist'",
                    (series_id,),
                ).fetchone()[0]
                aliases = {
                    tuple(row)
                    for row in db.execute(
                        "SELECT alias,source FROM series_aliases WHERE series_id=?",
                        (series_id,),
                    )
                }
                genres = {
                    tuple(row)
                    for row in db.execute(
                        "SELECT tag,source FROM series_tags WHERE series_id=?",
                        (series_id,),
                    )
                }
                source_health = dict(
                    db.execute(
                        "SELECT status,error FROM series_metadata_sources"
                        " WHERE series_id=? AND source='anilist'",
                        (series_id,),
                    ).fetchone()
                )

            assert dict(first) == {
                "anilist_id": 707,
                "mal_id": 1707,
                "mu_id": "mu-707",
                "description": "Initial provider description",
                "cover_url": "https://covers.invalid/acceptance.jpg",
                "status": "FINISHED",
                "pub_year": 2021,
                "total_volumes": 4,
                "total_chapters": None,
                "vol_count_source": "anilist",
                "chapter_count_source": "manual",
                "metadata_status": "healthy",
                "last_metadata_refresh": first["last_metadata_refresh"],
            }
            assert first["last_metadata_refresh"]
            assert confidence == pytest.approx(10 / 11)
            assert source_health == {"status": "healthy", "error": None}
            assert ("Acceptance Alternate", "anilist") in aliases
            assert genres == {("action", "anilist"), ("drama", "anilist")}
            assert providers["search"].await_count == 1
            assert providers["by_id"].await_count == 0

            # API creation without an AniList identity initializes chapter
            # counts conservatively. An explicit API clear relinquishes that
            # ownership so the later exact provider refresh can apply it.
            clear_chapter_count = await client.patch(
                f"/api/v1/series/{series_id}",
                json={"total_chapters": None},
                headers=api_headers,
            )
            assert clear_chapter_count.status_code == 200

            manual_title = "Operator Controlled Title"
            patched = await client.patch(
                f"/api/v1/series/{series_id}",
                json={"title": manual_title},
                headers=api_headers,
            )
            assert patched.status_code == 200, patched.text
            locked = _field_state(series_id, "title")
            assert locked["value"] == manual_title
            assert locked["selected_source"] == "manual"
            assert locked["locked"] is True

            refreshed = await client.post(
                f"/series/{series_id}/refresh",
                headers=csrf,
            )
            assert refreshed.status_code == 303, refreshed.text
            exact_locked = _field_state(series_id, "title")
            assert exact_locked["value"] == manual_title
            assert exact_locked["selected_source"] == "manual"
            assert exact_locked["locked"] is True
            assert exact_locked["pending"] is False
            anilist_candidate = next(
                candidate
                for candidate in exact_locked["candidates"]
                if candidate["source"] == "anilist"
            )
            assert anilist_candidate["value"] == "Provider Exact Title"
            assert anilist_candidate["confidence"] == 1.0
            assert providers["search"].await_count == 1
            providers["by_id"].assert_awaited_once_with(707)

            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                exact_metadata = db.execute(
                    "SELECT description,total_chapters,chapter_count_source"
                    " FROM series WHERE id=?",
                    (series_id,),
                ).fetchone()
            assert exact_metadata == (
                "Description refreshed by exact identity",
                40,
                "anilist",
            )

            unlocked_response = await client.post(
                f"/series/{series_id}/metadata/lock",
                data={"field_name": "title", "locked": "0"},
                headers=csrf,
            )
            assert unlocked_response.status_code == 303
            unlocked = _field_state(series_id, "title")
            assert unlocked["value"] == manual_title
            assert unlocked["selected_source"] == "manual"
            assert unlocked["locked"] is False
            assert unlocked["recommended"]["source"] == "anilist"
            assert unlocked["recommended"]["value"] == "Provider Exact Title"
            assert unlocked["pending"] is True
            assert unlocked["conflict"] is True  # API and AniList still disagree.
            assert {
                candidate["source"] for candidate in unlocked["candidates"]
            } >= {"api", "manual", "anilist"}

            applied = await client.post(
                f"/series/{series_id}/metadata/apply-candidate",
                data={
                    "field_name": "title",
                    "source": "anilist",
                    "allow_decrease": "0",
                },
                headers=csrf,
            )
            assert applied.status_code == 303
            selected = _field_state(series_id, "title")
            assert selected["value"] == "Provider Exact Title"
            assert selected["selected_source"] == "anilist"
            assert selected["locked"] is False

            current_description = "Description refreshed by exact identity"
            record_metadata_selections(
                series_id,
                {"description": current_description},
                {"description": "api"},
                locks={"description": False},
            )
            drift = _field_state(series_id, "description")
            assert drift["value"] == current_description
            assert drift["selected_source"] == "api"
            assert drift["pending"] is False
            assert drift["source_drift"] is True
            reconcile_response = await client.post(
                f"/series/{series_id}/metadata/apply-candidate",
                data={"field_name": "description", "source": "anilist"},
                headers=csrf,
            )
            assert reconcile_response.status_code == 303
            description = _field_state(series_id, "description")
            assert description["value"] == current_description
            assert description["selected_source"] == "anilist"
            assert description["source_drift"] is False

            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                pre_grab_volume = db.execute(
                    "SELECT id,status FROM volumes"
                    " WHERE series_id=? AND volume_num=4",
                    (series_id,),
                ).fetchone()
            assert pre_grab_volume is not None
            volume_id = int(pre_grab_volume[0])
            assert pre_grab_volume[1] == "wanted"

            release = {
                "url": "https://indexer.invalid/alpha-v04.torrent",
                "title": f"{discovery_title} v04 [Acceptance Group]",
                "protocol": "torrent",
                "guid": "acceptance-guid-v04",
                "indexer": "Acceptance indexer",
                "seeders": 5,
            }
            grab_url = AsyncMock(
                return_value=GrabResult(
                    True,
                    "qbittorrent",
                    "acceptance-download-v04",
                    True,
                    77,
                )
            )
            search_all_indexers = AsyncMock(return_value=[release.copy()])
            monkeypatch.setattr(
                indexers,
                "search_all_indexers",
                search_all_indexers,
            )
            monkeypatch.setattr(grab_core, "grab_url", grab_url)

            search_response = await client.get(
                f"/api/series/{series_id}/volumes/{volume_id}/search",
                headers=api_headers,
            )
            assert search_response.status_code == 200, search_response.text
            search_payload = search_response.json()
            assert search_payload["query"] == "Provider Exact Title v4"
            assert len(search_payload["results"]) == 1
            returned_release = search_payload["results"][0]
            assert {
                key: returned_release[key] for key in release
            } == release
            searched_queries = {
                awaited.args[1] for awaited in search_all_indexers.await_args_list
            }
            assert {
                "Provider Exact Title v4",
                "Provider Exact Title",
                f"{discovery_title} v4",
            } <= searched_queries
            assert all(
                awaited.kwargs
                == {"purpose": "interactive", "series_id": series_id}
                for awaited in search_all_indexers.await_args_list
            )

            grab_response = await client.post(
                f"/api/series/{series_id}/volumes/{volume_id}/grab-release",
                json=returned_release,
                headers=api_headers,
            )
            assert grab_response.status_code == 200, grab_response.text
            assert grab_response.json() == {"ok": True, "message": "Grabbed"}

            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                db.row_factory = sqlite3.Row
                grabbed = db.execute(
                    "SELECT status,download_id,download_client_id FROM volumes"
                    " WHERE series_id=? AND volume_num=4",
                    (series_id,),
                ).fetchone()
                seen = db.execute(
                    "SELECT release_guid,download_id,download_client_id FROM seen"
                    " WHERE torrent_url=?",
                    (release["url"],),
                ).fetchone()
            assert dict(grabbed) == {
                "status": "grabbed",
                "download_id": "acceptance-download-v04",
                "download_client_id": 77,
            }
            assert dict(seen) == {
                "release_guid": "acceptance-guid-v04",
                "download_id": "acceptance-download-v04",
                "download_client_id": 77,
            }

            completed_dir = lifecycle_env["completed_root"] / "alpha-v04"
            completed_cbz = completed_dir / f"{discovery_title} v04.cbz"
            _make_cbz(completed_cbz, b"scenario-a-page")
            with get_db() as db:
                queue_id, needs_review = _queue_import(
                    db,
                    series_id=series_id,
                    download_id="acceptance-download-v04",
                    torrent_name=release["title"],
                    torrent_url=release["url"],
                    volume_num=4.0,
                    content_path=str(completed_dir),
                )
            assert queue_id is not None
            assert needs_review is False
            await _process_auto_import(queue_id)

            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                db.row_factory = sqlite3.Row
                imported = db.execute(
                    "SELECT id,status,import_path,download_id,download_client_id"
                    " FROM volumes WHERE series_id=? AND volume_num=4",
                    (series_id,),
                ).fetchone()
                queue = db.execute(
                    "SELECT status,series_id,download_id,download_client_id"
                    " FROM import_queue WHERE id=?",
                    (queue_id,),
                ).fetchone()
                publication = db.execute(
                    "SELECT state,result_ok,result_imported_count,"
                    " result_queue_status,queue_download_client_id"
                    " FROM import_publications WHERE queue_id=?",
                    (queue_id,),
                ).fetchone()
                history = db.execute(
                    "SELECT event_type,download_id,download_client_id"
                    " FROM history WHERE series_id=? AND event_type='imported'",
                    (series_id,),
                ).fetchone()
            imported_path = Path(imported["import_path"])
            assert imported["status"] == "downloaded"
            assert imported["download_id"] == "acceptance-download-v04"
            assert imported["download_client_id"] == 77
            assert imported_path.is_file()
            assert imported_path.is_relative_to(lifecycle_env["library_root"])
            assert queue is None
            assert publication["state"] in {"finalized", "deleted"}
            assert publication["result_ok"] == 1
            assert publication["result_imported_count"] == 1
            assert publication["result_queue_status"] == "imported"
            assert publication["queue_download_client_id"] == 77
            assert dict(history) == {
                "event_type": "imported",
                "download_id": "acceptance-download-v04",
                "download_client_id": 77,
            }

            before_rescan = {
                "path": str(imported_path),
                "digest": _digest(imported_path),
                "identity": (707, 1707, "mu-707"),
                "title_source": ("anilist", 0),
            }
            rescanned = await client.post(
                f"/series/{series_id}/rescan",
                headers=csrf,
            )
            assert rescanned.status_code == 303
            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                db.row_factory = sqlite3.Row
                after_rescan = db.execute(
                    "SELECT status,import_path FROM volumes"
                    " WHERE series_id=? AND volume_num=4",
                    (series_id,),
                ).fetchone()
                identities = db.execute(
                    "SELECT anilist_id,mal_id,mu_id FROM series WHERE id=?",
                    (series_id,),
                ).fetchone()
                title_selection = db.execute(
                    "SELECT selected_source,locked FROM series_metadata_fields"
                    " WHERE series_id=? AND field_name='title'",
                    (series_id,),
                ).fetchone()
                duplicate_volumes = db.execute(
                    "SELECT volume_num,COUNT(*) FROM volumes WHERE series_id=?"
                    " GROUP BY volume_num HAVING COUNT(*)>1",
                    (series_id,),
                ).fetchall()
            assert dict(after_rescan) == {
                "status": "downloaded",
                "import_path": before_rescan["path"],
            }
            assert tuple(identities) == before_rescan["identity"]
            assert tuple(title_selection) == before_rescan["title_source"]
            assert duplicate_volumes == []

            providers["by_id"].return_value = _anilist_record(
                title="Provider Exact Title",
                description="Lower-count refresh after import",
                volumes=2,
                chapters=20,
            )
            providers["mu_search"].return_value = [
                {
                    "title": "Provider Exact Title",
                    "mu_id": "foreign-same-title",
                    "volumes": 99,
                },
                {
                    "title": "Provider Exact Title",
                    "mu_id": "mu-707",
                    "volumes": 2,
                },
            ]
            final_refresh = await client.post(
                f"/series/{series_id}/refresh",
                headers=csrf,
            )
            assert final_refresh.status_code == 303
            final_title = _field_state(series_id, "title")

            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                db.row_factory = sqlite3.Row
                final_series = db.execute(
                    "SELECT title,anilist_id,mal_id,mu_id,total_volumes,"
                    " total_chapters,vol_count_source,description FROM series"
                    " WHERE id=?",
                    (series_id,),
                ).fetchone()
                final_volume = db.execute(
                    "SELECT status,import_path FROM volumes"
                    " WHERE series_id=? AND volume_num=4",
                    (series_id,),
                ).fetchone()
                mu_candidate = db.execute(
                    "SELECT value_json FROM series_metadata_candidates"
                    " WHERE series_id=? AND field_name='total_volumes'"
                    " AND source='mangaupdates'",
                    (series_id,),
                ).fetchone()[0]
                local_candidate = db.execute(
                    "SELECT value_json FROM series_metadata_candidates"
                    " WHERE series_id=? AND field_name='total_volumes'"
                    " AND source='local'",
                    (series_id,),
                ).fetchone()[0]
            assert dict(final_series) == {
                "title": "Provider Exact Title",
                "anilist_id": 707,
                "mal_id": 1707,
                "mu_id": "mu-707",
                "total_volumes": 4,
                "total_chapters": 40,
                "vol_count_source": "anilist",
                "description": "Lower-count refresh after import",
            }
            assert dict(final_volume) == {
                "status": "downloaded",
                "import_path": str(imported_path),
            }
            assert mu_candidate == "2"
            assert local_candidate == "4"
            assert final_title["value"] == "Provider Exact Title"
            assert final_title["selected_source"] == "anilist"
            assert final_title["locked"] is False
            assert _digest(imported_path) == before_rescan["digest"]

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))


def test_scenario_b_explicit_adoption_keeps_pinned_folder_through_title_change(
    lifecycle_env: LifecycleEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted provider title must not orphan an explicitly adopted folder."""
    import main
    import rescan
    from routers import api_v1

    adopted_folder = lifecycle_env["library_root"] / "Adopted Shelf"
    adopted_file = adopted_folder / "Adopted Shelf v02.cbz"
    _make_cbz(adopted_file, b"scenario-b-page")
    proposals = [
        {
            "title": "Adopted Shelf",
            "source": "anilist",
            "anilist_id": 111,
            "mal_id": 211,
            "mu_id": None,
            "cover_url": "https://covers.invalid/wrong.jpg",
            "status": "FINISHED",
            "volumes": 2,
            "chapters": 20,
            "pub_year": 2019,
            "description": "First identity",
        },
        {
            "title": "Adopted Shelf",
            "source": "anilist",
            "anilist_id": 222,
            "mal_id": 322,
            "mu_id": None,
            "cover_url": "https://covers.invalid/chosen.jpg",
            "status": "FINISHED",
            "volumes": 3,
            "chapters": 24,
            "pub_year": 2020,
            "description": "Explicitly chosen identity",
        },
    ]
    search_series = AsyncMock(return_value=(proposals, "anilist"))
    monkeypatch.setattr(api_v1, "search_series", search_series)
    exact_record = _anilist_record(
        anilist_id=222,
        mal_id=322,
        title="Canonical Adopted Title",
        aliases=["Adopted Shelf Alt"],
        genres=["Adventure"],
        cover_url="https://covers.invalid/chosen.jpg",
        volumes=3,
        chapters=24,
        pub_year=2020,
        description="Adopted provider refresh",
    )
    providers = _install_provider_doubles(
        monkeypatch,
        search_results=[exact_record],
        exact_record=exact_record,
        chapter_map=_chapter_map(24, 3),
    )

    async def scenario() -> None:
        csrf_token = "csrf-milestone-b-" + "b" * 32
        api_headers = {"X-Api-Key": lifecycle_env["api_key"]}
        csrf = {"X-CSRFToken": csrf_token}

        async for client in _client(main.app):
            client.cookies.set("csrftoken", csrf_token)
            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                before_series = db.execute("SELECT COUNT(*) FROM series").fetchone()[0]
                before_volumes = db.execute("SELECT COUNT(*) FROM volumes").fetchone()[0]

            matches = await client.get(
                "/api/v1/rootfolder/1/unmappedfolders/matches",
                params={"path": str(adopted_folder), "query": "Adopted Shelf"},
                headers=api_headers,
            )
            assert matches.status_code == 200, matches.text
            match_body = matches.json()
            assert match_body["matchState"] == "ambiguous"
            assert match_body["topMatchCount"] == 2
            assert {item["anilistId"] for item in match_body["matches"]} == {
                111,
                222,
            }
            search_series.assert_awaited_once_with("Adopted Shelf")
            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                assert db.execute("SELECT COUNT(*) FROM series").fetchone()[0] == before_series
                assert db.execute("SELECT COUNT(*) FROM volumes").fetchone()[0] == before_volumes
            with zipfile.ZipFile(adopted_file) as archive:
                assert archive.read("001.jpg") == b"scenario-b-page"

            adopted = await asyncio.wait_for(
                client.post(
                    "/api/v1/rootfolder/1/unmappedfolders/adopt",
                    json={
                        "path": str(adopted_folder),
                        "metadataTitle": "Canonical Adopted Title",
                        "anilistId": 222,
                        "malId": 322,
                        "coverUrl": "https://covers.invalid/chosen.jpg",
                        "status": "FINISHED",
                        "overview": "Explicitly chosen identity",
                        "totalVolumes": 3,
                        "totalChapters": 24,
                        "year": 2020,
                        "metadataSource": "anilist",
                    },
                    headers=api_headers,
                ),
                timeout=5,
            )
            assert adopted.status_code == 200, adopted.text
            series_id = int(adopted.json()["series"]["id"])
            await asyncio.wait_for(
                _wait_for_background_task(f"adopt_series:{series_id}:metadata"),
                timeout=5,
            )

            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                db.row_factory = sqlite3.Row
                series = db.execute(
                    "SELECT title,search_pattern,folder_name,root_folder_id,"
                    " anilist_id,mal_id FROM series WHERE id=?",
                    (series_id,),
                ).fetchone()
                volumes = db.execute(
                    "SELECT volume_num,status,import_path FROM volumes"
                    " WHERE series_id=? ORDER BY volume_num",
                    (series_id,),
                ).fetchall()
                mapped_path = rescan._series_library_dir(db, series_id)
            assert dict(series) == {
                "title": "Adopted Shelf",
                "search_pattern": "Canonical Adopted Title",
                "folder_name": "Adopted Shelf",
                "root_folder_id": 1,
                "anilist_id": 222,
                "mal_id": 322,
            }
            assert mapped_path == str(adopted_folder)
            assert [row["volume_num"] for row in volumes] == [1.0, 2.0, 3.0]
            assert [row["status"] for row in volumes] == [
                "wanted",
                "downloaded",
                "wanted",
            ]
            assert volumes[1]["import_path"] == str(adopted_file)

            locked = _field_state(series_id, "title")
            assert locked["value"] == "Adopted Shelf"
            assert locked["selected_source"] == "local"
            assert locked["locked"] is True
            assert locked["pending"] is False
            assert locked["conflict"] is True
            assert locked["recommended"]["source"] == "local"
            assert next(
                candidate
                for candidate in locked["candidates"]
                if candidate["source"] == "anilist"
            )["value"] == "Canonical Adopted Title"
            providers["by_id"].assert_awaited_once_with(222)

            unlocked_response = await client.post(
                f"/series/{series_id}/metadata/lock",
                data={"field_name": "title", "locked": "0"},
                headers=csrf,
            )
            assert unlocked_response.status_code == 303
            unlocked = _field_state(series_id, "title")
            assert unlocked["value"] == "Adopted Shelf"
            assert unlocked["selected_source"] == "local"
            assert unlocked["locked"] is False
            assert unlocked["recommended"]["source"] == "anilist"
            assert unlocked["recommended"]["value"] == "Canonical Adopted Title"
            assert unlocked["pending"] is True
            assert unlocked["conflict"] is False
            assert {
                candidate["source"]: candidate["value"]
                for candidate in unlocked["candidates"]
            } == {
                "anilist": "Canonical Adopted Title",
                "local": "Adopted Shelf",
            }

            applied = await client.post(
                f"/series/{series_id}/metadata/apply-candidate",
                data={"field_name": "title", "source": "anilist"},
                headers=csrf,
            )
            assert applied.status_code == 303
            selected = _field_state(series_id, "title")
            assert selected["value"] == "Canonical Adopted Title"
            assert selected["selected_source"] == "anilist"
            assert selected["locked"] is False

            rescanned = await client.post(
                f"/series/{series_id}/rescan",
                headers=csrf,
            )
            assert rescanned.status_code == 303
            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                db.row_factory = sqlite3.Row
                final_series = db.execute(
                    "SELECT title,folder_name,anilist_id,mal_id FROM series"
                    " WHERE id=?",
                    (series_id,),
                ).fetchone()
                final_volume = db.execute(
                    "SELECT status,import_path FROM volumes"
                    " WHERE series_id=? AND volume_num=2",
                    (series_id,),
                ).fetchone()
                series_count = db.execute(
                    "SELECT COUNT(*) FROM series WHERE deleted_at IS NULL"
                ).fetchone()[0]
                duplicate_volumes = db.execute(
                    "SELECT volume_num,COUNT(*) FROM volumes WHERE series_id=?"
                    " GROUP BY volume_num HAVING COUNT(*)>1",
                    (series_id,),
                ).fetchall()
                mapped_path = rescan._series_library_dir(db, series_id)
            assert dict(final_series) == {
                "title": "Canonical Adopted Title",
                "folder_name": "Adopted Shelf",
                "anilist_id": 222,
                "mal_id": 322,
            }
            assert dict(final_volume) == {
                "status": "downloaded",
                "import_path": str(adopted_file),
            }
            assert mapped_path == str(adopted_folder)
            assert series_count == before_series + 1
            assert duplicate_volumes == []
            with zipfile.ZipFile(adopted_file) as archive:
                assert archive.read("001.jpg") == b"scenario-b-page"

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))


def test_scenario_c_ambiguous_refresh_preserves_cache_until_operator_identity(
    lifecycle_env: LifecycleEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous title-only identity must fail closed across DB and disk state."""
    import main
    import metadata_service
    from metadata_provenance import record_metadata_candidates

    ambiguous_results = [
        _anilist_record(
            anilist_id=901,
            mal_id=1901,
            title="Cache Safe Series",
            description="Foreign candidate one",
        ),
        _anilist_record(
            anilist_id=902,
            mal_id=1902,
            title="Cache Safe Series",
            description="Foreign candidate two",
        ),
    ]
    exact_record = _anilist_record(
        anilist_id=909,
        mal_id=1909,
        title="Cache Safe Series",
        cover_url="https://covers.invalid/resolved.jpg",
        description="Operator-resolved metadata",
        volumes=1,
        chapters=10,
        pub_year=2024,
    )
    providers = _install_provider_doubles(
        monkeypatch,
        search_results=ambiguous_results,
        exact_record=exact_record,
        chapter_map=_chapter_map(10, 1),
    )

    async def scenario() -> None:
        csrf_token = "csrf-milestone-c-" + "c" * 32
        api_headers = {"X-Api-Key": lifecycle_env["api_key"]}
        csrf = {"X-CSRFToken": csrf_token}

        async for client in _client(main.app):
            client.cookies.set("csrftoken", csrf_token)
            created = await client.post(
                "/api/v1/series",
                json={
                    "title": "Cache Safe Series",
                    "searchPattern": "Cache Safe Series",
                    "coverUrl": "https://covers.invalid/cached.jpg",
                    "overview": "Cached description",
                    "status": "RELEASING",
                    "totalVolumes": 1,
                    "totalChapters": 10,
                    "year": 2018,
                    "rootFolderId": 1,
                },
                headers=api_headers,
            )
            assert created.status_code == 200, created.text
            series_id = int(created.json()["series"]["id"])
            await _wait_for_background_task(f"api_series:{series_id}:metadata")

            series_folder = lifecycle_env["library_root"] / "Cache Safe Series"
            cached_file = series_folder / "Cache Safe Series v01.cbz"
            _make_cbz(cached_file, b"scenario-c-page")
            rescanned = await client.post(
                f"/series/{series_id}/rescan",
                headers=csrf,
            )
            assert rescanned.status_code == 303
            cached_digest = _digest(cached_file)

            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                db.row_factory = sqlite3.Row
                before = db.execute(
                    "SELECT anilist_id,mal_id,cover_url,description,status,pub_year,"
                    " total_volumes,total_chapters FROM series WHERE id=?",
                    (series_id,),
                ).fetchone()
                volume_before = db.execute(
                    "SELECT status,import_path FROM volumes"
                    " WHERE series_id=? AND volume_num=1",
                    (series_id,),
                ).fetchone()
                candidate_count_before = db.execute(
                    "SELECT COUNT(*) FROM series_metadata_candidates"
                    " WHERE series_id=? AND source='anilist'",
                    (series_id,),
                ).fetchone()[0]
            assert dict(before) == {
                "anilist_id": None,
                "mal_id": None,
                "cover_url": "https://covers.invalid/cached.jpg",
                "description": "Cached description",
                "status": "RELEASING",
                "pub_year": 2018,
                "total_volumes": 1,
                "total_chapters": 10,
            }
            assert dict(volume_before) == {
                "status": "downloaded",
                "import_path": str(cached_file),
            }
            assert candidate_count_before == 0

            failed = await metadata_service.refresh_series_metadata(
                series_id,
                force=True,
                include_manifest=False,
                reason="milestone_ambiguous_cache",
            )
            assert failed["ok"] is False
            assert failed["status"] == "failed"
            assert "identity ambiguity" in failed["errors"][0]

            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                db.row_factory = sqlite3.Row
                after_failure = db.execute(
                    "SELECT anilist_id,mal_id,cover_url,description,status,pub_year,"
                    " total_volumes,total_chapters,metadata_status,metadata_error"
                    " FROM series WHERE id=?",
                    (series_id,),
                ).fetchone()
                source_failure = db.execute(
                    "SELECT status,error,details FROM series_metadata_sources"
                    " WHERE series_id=? AND source='anilist'",
                    (series_id,),
                ).fetchone()
                rejected_candidates = db.execute(
                    "SELECT COUNT(*) FROM series_metadata_candidates"
                    " WHERE series_id=? AND source='anilist'",
                    (series_id,),
                ).fetchone()[0]
                volume_after_failure = db.execute(
                    "SELECT status,import_path FROM volumes"
                    " WHERE series_id=? AND volume_num=1",
                    (series_id,),
                ).fetchone()
            assert dict(after_failure) == {
                **dict(before),
                "metadata_status": "failed",
                "metadata_error": after_failure["metadata_error"],
            }
            assert "identity ambiguity" in after_failure["metadata_error"]
            assert source_failure["status"] == "failed"
            assert "identity ambiguity" in source_failure["error"]
            assert '"reason":"identity_ambiguous"' in source_failure["details"]
            assert rejected_candidates == 0
            assert dict(volume_after_failure) == dict(volume_before)
            assert cached_file.is_file()
            assert _digest(cached_file) == cached_digest

            record_metadata_candidates(
                series_id,
                "anilist",
                {"anilist_id": 909},
                confidence=1.0,
            )
            resolved = await client.post(
                f"/series/{series_id}/metadata/apply-candidate",
                data={"field_name": "anilist_id", "source": "anilist"},
                headers=csrf,
            )
            assert resolved.status_code == 303
            assert _field_state(series_id, "anilist_id")["value"] == 909

            succeeded = await metadata_service.refresh_series_metadata(
                series_id,
                force=True,
                include_manifest=False,
                reason="milestone_operator_identity",
            )
            assert succeeded["ok"] is True
            assert succeeded["status"] == "healthy"
            providers["by_id"].assert_awaited_once_with(909)
            assert providers["search"].await_count == 2

            with sqlite3.connect(lifecycle_env["db_path"]) as db:
                db.row_factory = sqlite3.Row
                final = db.execute(
                    "SELECT anilist_id,mal_id,description,cover_url,pub_year,"
                    " metadata_status,metadata_error FROM series WHERE id=?",
                    (series_id,),
                ).fetchone()
                final_volume = db.execute(
                    "SELECT status,import_path FROM volumes"
                    " WHERE series_id=? AND volume_num=1",
                    (series_id,),
                ).fetchone()
                exact_candidate = db.execute(
                    "SELECT value_json,confidence FROM series_metadata_candidates"
                    " WHERE series_id=? AND field_name='description'"
                    " AND source='anilist'",
                    (series_id,),
                ).fetchone()
            assert dict(final) == {
                "anilist_id": 909,
                "mal_id": 1909,
                "description": "Operator-resolved metadata",
                "cover_url": "https://covers.invalid/resolved.jpg",
                "pub_year": 2024,
                "metadata_status": "healthy",
                "metadata_error": None,
            }
            assert dict(final_volume) == dict(volume_before)
            assert dict(exact_candidate) == {
                "value_json": '"Operator-resolved metadata"',
                "confidence": 1.0,
            }
            assert _digest(cached_file) == cached_digest

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))
