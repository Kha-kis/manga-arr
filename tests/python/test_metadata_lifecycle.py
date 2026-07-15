"""Unified metadata lifecycle regression coverage."""
import asyncio
import os
import sqlite3
import tempfile
from unittest.mock import AsyncMock, patch

import pytest


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@pytest.fixture
def db_path():
    import main, security, shared

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-metadata-keys-")
    old_main = main.DB_PATH
    old_shared = shared.DB_PATH
    main.DB_PATH = tmp.name
    shared.DB_PATH = tmp.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    try:
        yield tmp.name
    finally:
        main.DB_PATH = old_main
        shared.DB_PATH = old_shared
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(tmp.name + suffix)
            except FileNotFoundError:
                pass


def _seed_series(db_path, **overrides):
    values = {
        "id": 7,
        "title": "Exact Series",
        "search_pattern": "Exact Series",
        "anilist_id": 123,
        "total_volumes": 12,
        "total_chapters": 1,
        "vol_count_source": "manual",
        "chapter_count_source": "anilist",
        "edition_type": "standard",
        "status": "RELEASING",
        "update_strategy": "always",
        "monitored": 1,
    }
    values.update(overrides)
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    with sqlite3.connect(db_path) as db:
        db.execute(
            f"INSERT INTO series({columns}) VALUES({placeholders})",
            tuple(values.values()),
        )


def _anilist_record(**overrides):
    record = {
        "anilist_id": 123,
        "mal_id": 456,
        "title": "Exact Series",
        "romaji_title": "Exact Series Romaji",
        "aliases": ["Exact Alternate"],
        "genres": ["Action", "Drama"],
        "cover_url": "https://cdn.example.test/new.jpg",
        "status": "FINISHED",
        "format": "MANGA",
        "volumes": 5,
        "chapters": 6,
        "pub_year": 2020,
        "description": "Canonical description",
        "source": "anilist",
    }
    record.update(overrides)
    return record


def test_refresh_uses_exact_id_and_preserves_manual_counts(db_path):
    import metadata_service as service

    _seed_series(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO chapters(series_id,chapter_num,status)"
            " VALUES(7,8,'downloaded')"
        )
        db.executemany(
            "INSERT INTO series_aliases(series_id,alias,source) VALUES(7,?,?)",
            [
                ("Exact Alternate", "manual"),
                ("Old AniList Alias", "anilist"),
                ("Operator Alias", "manual"),
            ],
        )
        db.executemany(
            "INSERT INTO series_tags(series_id,tag,source) VALUES(7,?,?)",
            [
                ("action", "manual"),
                ("old-provider-tag", "anilist"),
                ("operator-tag", "manual"),
            ],
        )

    by_id = AsyncMock(return_value=_anilist_record())
    with (
        patch.object(service, "fetch_anilist_by_id", by_id),
        patch.object(service, "refresh_mangadex_map", AsyncMock(return_value=True)),
        patch.object(
            service, "refresh_series_cover", AsyncMock(return_value=(True, None))
        ),
    ):
        result = _run(
            service.refresh_series_metadata(
                7, force=True, include_manifest=False, reason="test"
            )
        )

    assert result["status"] == "healthy"
    by_id.assert_awaited_once_with(123)
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        series = db.execute("SELECT * FROM series WHERE id=7").fetchone()
        aliases = db.execute(
            "SELECT alias,source FROM series_aliases WHERE series_id=7 ORDER BY alias"
        ).fetchall()
        tags = db.execute(
            "SELECT tag,source FROM series_tags WHERE series_id=7 ORDER BY tag"
        ).fetchall()

    assert series["total_volumes"] == 12
    assert series["vol_count_source"] == "manual"
    assert series["total_chapters"] == 8
    assert series["chapter_count_source"] == "local"
    assert series["description"] == "Canonical description"
    assert series["cover_url"] == "https://cdn.example.test/new.jpg"
    assert series["last_metadata_refresh"]
    assert series["metadata_status"] == "healthy"
    assert series["update_strategy"] == "once"
    assert [(row["alias"], row["source"]) for row in aliases] == [
        ("Exact Alternate", "manual"),
        ("Exact Series Romaji", "anilist"),
        ("Operator Alias", "manual"),
    ]
    assert [(row["tag"], row["source"]) for row in tags] == [
        ("action", "manual"),
        ("drama", "anilist"),
        ("operator-tag", "manual"),
    ]


class _MangaDexLookupResponse:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": self._data}


class _MangaDexLookupClient:
    responses = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return _MangaDexLookupResponse(
            self.responses.get(kwargs.get("params", {}).get("title"), [])
        )


def test_mangadex_short_query_requires_identity_or_full_title_confidence():
    import metadata

    exact = {
        "id": "mdx-exact",
        "attributes": {
            "title": {"en": "Uzumaki"},
            "altTitles": [],
            "links": {"al": "123"},
        },
    }
    unrelated = {
        "id": "mdx-unrelated",
        "attributes": {
            "title": {"en": "A Different Work"},
            "altTitles": [],
            "links": {},
        },
    }
    _MangaDexLookupClient.responses = {
        "Uzumaki: Spiral into Horror": [],
        "Uzumaki": [unrelated, exact],
    }
    with patch.object(metadata.httpx, "AsyncClient", _MangaDexLookupClient):
        manga_id, links = _run(
            metadata.fetch_mangadex_id(
                "Uzumaki: Spiral into Horror", anilist_id=123
            )
        )
    assert manga_id == "mdx-exact"
    assert links["al"] == "123"

    _MangaDexLookupClient.responses = {
        "Original Series: Deluxe": [unrelated],
        "Original Series": [
            {
                "id": "base-work",
                "attributes": {
                    "title": {"en": "Original Series"},
                    "altTitles": [],
                    "links": {},
                },
            }
        ],
    }
    with patch.object(metadata.httpx, "AsyncClient", _MangaDexLookupClient):
        manga_id, links = _run(
            metadata.fetch_mangadex_id("Original Series: Deluxe", anilist_id=None)
        )
    assert manga_id is None
    assert links == {}


class _KitsuResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _KitsuClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, *args, **kwargs):
        if url.endswith("/manga"):
            return _KitsuResponse(
                {
                    "data": [
                        {
                            "id": "wrong",
                            "attributes": {
                                "canonicalTitle": "A Different Work",
                                "titles": {},
                                "chapterCount": 1000,
                            },
                        },
                        {
                            "id": "right",
                            "attributes": {
                                "canonicalTitle": "One Piece",
                                "titles": {"en": "One Piece"},
                                "chapterCount": 1000,
                            },
                        },
                    ]
                }
            )
        assert kwargs["params"]["filter[manga_id]"] == "right"
        return _KitsuResponse(
            {
                "data": [
                    {
                        "attributes": {"number": "1", "volumeNumber": "1"}
                    }
                ],
                "links": {},
            }
        )


def test_kitsu_map_requires_confident_title_identity():
    import metadata

    with patch.object(metadata.httpx, "AsyncClient", _KitsuClient):
        mapping = _run(
            metadata.fetch_kitsu_chapter_map(
                "One Piece (Official Color)", anilist_id=30013, total_chapters=1000
            )
        )
    assert mapping == {"1": 1}


def test_failed_core_refresh_keeps_last_success(db_path):
    import metadata_service as service
    from metadata import MetadataProviderError

    old_success = "2026-01-01T00:00:00+00:00"
    _seed_series(db_path, last_metadata_refresh=old_success)
    with (
        patch.object(
            service,
            "fetch_anilist_by_id",
            AsyncMock(side_effect=MetadataProviderError("provider offline")),
        ),
        patch.object(service, "refresh_mangadex_map", AsyncMock(return_value=True)),
        patch.object(
            service, "refresh_series_cover", AsyncMock(return_value=(True, None))
        ),
    ):
        result = _run(
            service.refresh_series_metadata(
                7, force=True, include_manifest=False, reason="test"
            )
        )

    assert result["status"] == "failed"
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT metadata_status,last_metadata_refresh,metadata_error"
            " FROM series WHERE id=7"
        ).fetchone()
        source = db.execute(
            "SELECT status,failure_count,next_retry_at,error"
            " FROM series_metadata_sources WHERE series_id=7 AND source='anilist'"
        ).fetchone()
    assert series["metadata_status"] == "failed"
    assert series["last_metadata_refresh"] == old_success
    assert "provider offline" in series["metadata_error"]
    assert source["status"] == "failed"
    assert source["failure_count"] == 1
    assert source["next_retry_at"]


def test_optional_provider_outage_is_recorded_as_degraded(db_path):
    import metadata_service as service

    _seed_series(db_path, vol_count_source="anilist")
    with (
        patch.object(
            service, "fetch_anilist_by_id", AsyncMock(return_value=_anilist_record())
        ),
        patch.object(
            service,
            "fetch_mu_metadata",
            AsyncMock(side_effect=RuntimeError("provider offline")),
        ),
        patch.object(service, "refresh_mangadex_map", AsyncMock(return_value=True)),
        patch.object(
            service, "refresh_series_cover", AsyncMock(return_value=(True, None))
        ),
    ):
        result = _run(
            service.refresh_series_metadata(
                7, force=True, include_manifest=False, reason="test"
            )
        )

    assert result["status"] == "degraded"
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT total_volumes,metadata_status FROM series WHERE id=7"
        ).fetchone()
        source = db.execute(
            "SELECT status,failure_count,error FROM series_metadata_sources"
            " WHERE series_id=7 AND source='mangaupdates'"
        ).fetchone()
    assert series["total_volumes"] == 12
    assert series["metadata_status"] == "degraded"
    assert source["status"] == "failed"
    assert source["failure_count"] == 1
    assert "provider offline" in source["error"]


def test_init_recovers_interrupted_refresh_state(db_path):
    import main

    old_success = "2026-01-01T00:00:00+00:00"
    _seed_series(
        db_path,
        metadata_status="refreshing",
        last_metadata_refresh=old_success,
    )
    with sqlite3.connect(db_path) as db:
        db.executemany(
            "INSERT INTO series_metadata_sources"
            " (series_id,source,status,last_attempt_at,last_success_at)"
            " VALUES(7,?,'refreshing','2026-01-02',?)",
            [("anilist", old_success), ("cover", None)],
        )

    main.init_db()

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT metadata_status,metadata_error FROM series WHERE id=7"
        ).fetchone()
        sources = db.execute(
            "SELECT source,status,error FROM series_metadata_sources"
            " WHERE series_id=7 ORDER BY source"
        ).fetchall()
    assert series["metadata_status"] == "degraded"
    assert "interrupted" in series["metadata_error"]
    assert [(row["source"], row["status"]) for row in sources] == [
        ("anilist", "degraded"),
        ("cover", "pending"),
    ]
    assert all("interrupted" in row["error"] for row in sources)


@pytest.mark.parametrize(
    ("existing_source", "expected_source"),
    [("mangadex", "mangadex"), (None, "legacy")],
)
def test_map_failure_preserves_last_known_good_map(
    db_path, existing_source, expected_source
):
    import metadata_enrichment as enrichment

    old_map = '{"1": 1, "2": 1, "6": 2}'
    _seed_series(
        db_path,
        mangadex_id="mdx-1",
        mal_id=456,
        mu_id="789",
        chapter_vol_map=old_map,
        chapter_map_source=existing_source,
    )
    with (
        patch.object(
            enrichment, "fetch_chapter_volume_map", AsyncMock(return_value={})
        ),
        patch.object(enrichment, "fetch_kitsu_chapter_map", AsyncMock(return_value={})),
        patch.object(enrichment, "_extract_map_from_cbzs", return_value={}),
    ):
        result = _run(enrichment.refresh_mangadex_map(7))

    assert result is False
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        series = db.execute(
            "SELECT chapter_vol_map,chapter_map_source FROM series WHERE id=7"
        ).fetchone()
        source = db.execute(
            "SELECT status,error,details FROM series_metadata_sources"
            " WHERE series_id=7 AND source='chapter_map'"
        ).fetchone()
    assert series["chapter_vol_map"] == old_map
    assert series["chapter_map_source"] == expected_source
    assert source["status"] == "degraded"
    assert "preserved" in source["error"]
    assert expected_source in source["details"]


class _CoverResponse:
    headers = {}

    def __init__(self, payload):
        self.content = payload
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_bytes(self):
        yield self.content

    def raise_for_status(self):
        return None


class _CoverClient:
    payload = b""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return _CoverResponse(self.payload)

    def stream(self, *args, **kwargs):
        return _CoverResponse(self.payload)


def test_cover_refresh_is_validated_and_atomic(tmp_path):
    import cover_images
    from io import BytesIO
    from PIL import Image

    old_dir = cover_images.COVERS_DIR
    old_limit = cover_images.MAX_SOURCE_BYTES
    cover_images.COVERS_DIR = str(tmp_path)
    dest = tmp_path / "7.jpg"
    dest.write_bytes(b"\xff\xd8\xffold")
    png_buffer = BytesIO()
    Image.new("RGBA", (20, 30), (10, 20, 30, 128)).save(
        png_buffer, format="PNG"
    )
    png = png_buffer.getvalue()
    try:
        _CoverClient.payload = png
        with (
            patch.object(cover_images.httpx, "AsyncClient", _CoverClient),
            patch("security.validate_outbound_url", return_value=None),
        ):
            result = _run(
                cover_images.download_cover(
                    7, "https://cdn.example.test/cover.png", force=False
                )
            )
        assert result["ok"] is True
        normalized = dest.read_bytes()
        assert normalized.startswith(b"\xff\xd8\xff")
        assert result["format"] == "jpeg"
        assert result["source_format"] == "png"

        _CoverClient.payload = b"<html>upstream error</html>"
        with (
            patch.object(cover_images.httpx, "AsyncClient", _CoverClient),
            patch("security.validate_outbound_url", return_value=None),
        ):
            result = _run(
                cover_images.download_cover(
                    7, "https://cdn.example.test/cover.png", force=True
                )
            )
        assert result["ok"] is False
        assert result["status"] == "invalid_image"
        assert dest.read_bytes() == normalized

        cover_images.MAX_SOURCE_BYTES = 32
        _CoverClient.payload = b"x" * 33
        with (
            patch.object(cover_images.httpx, "AsyncClient", _CoverClient),
            patch("security.validate_outbound_url", return_value=None),
        ):
            result = _run(
                cover_images.download_cover(
                    7, "https://cdn.example.test/cover.png", force=True
                )
            )
        assert result["ok"] is False
        assert result["status"] == "invalid_image"
        assert dest.read_bytes() == normalized
    finally:
        cover_images.MAX_SOURCE_BYTES = old_limit
        cover_images.COVERS_DIR = old_dir


def test_metadata_retry_candidates_honor_backoff_and_monitoring(db_path):
    import metadata_state

    _seed_series(db_path)
    assert metadata_state.metadata_retry_candidates() == [7]

    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE series SET metadata_status='healthy' WHERE id=7")
        db.execute(
            "INSERT INTO series_metadata_sources"
            " (series_id,source,status,next_retry_at,failure_count)"
            " VALUES(7,'cover','failed',datetime('now','+1 hour'),1)"
        )
    assert metadata_state.metadata_retry_candidates() == []

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE series_metadata_sources SET next_retry_at=datetime('now','-1 minute')"
            " WHERE series_id=7 AND source='cover'"
        )
    assert metadata_state.metadata_retry_candidates() == [7]

    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE series SET monitored=0 WHERE id=7")
    assert metadata_state.metadata_retry_candidates() == []


def test_cbz_cover_is_normalized_to_jpeg(tmp_path):
    import cover_images
    from io import BytesIO
    from PIL import Image
    import zipfile

    png_buffer = BytesIO()
    Image.new("RGB", (24, 36), "red").save(png_buffer, format="PNG")
    archive = tmp_path / "volume.cbz"
    with zipfile.ZipFile(archive, "w") as cbz:
        cbz.writestr("001.png", png_buffer.getvalue())

    old_dir = cover_images.COVERS_DIR
    cover_images.COVERS_DIR = str(tmp_path / "covers")
    try:
        assert cover_images.extract_cbz_cover(8, str(archive)) is True
        dest = tmp_path / "covers" / "8.jpg"
        assert dest.read_bytes().startswith(b"\xff\xd8\xff")
        assert cover_images.cached_cover_is_valid(str(dest)) is True
    finally:
        cover_images.COVERS_DIR = old_dir


class _ManifestResponse:
    status_code = 200
    headers = {}

    def __init__(self, item):
        self._item = item

    def json(self):
        return {"data": [self._item], "total": 1}

    def raise_for_status(self):
        return None


class _ManifestClient:
    item = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return _ManifestResponse(self.item)


def test_manifest_sync_removes_stale_rows_and_enriches_chapters(db_path):
    from routers import mangadex_

    _seed_series(db_path, mangadex_id="mdx-1", ddl_language="en")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO chapters(series_id,chapter_num,title) VALUES(7,1,NULL)"
        )
        db.execute(
            "INSERT INTO mangadex_chapters"
            " (series_id,mangadex_chapter_id,chapter_num,language)"
            " VALUES(7,'stale',99,'en')"
        )

    _ManifestClient.item = {
        "id": "current",
        "attributes": {
            "chapter": "1",
            "volume": "1",
            "title": "The Beginning",
            "pages": 24,
            "translatedLanguage": "en",
            "publishAt": "2026-01-01T00:00:00Z",
        },
        "relationships": [],
    }
    with patch.object(mangadex_.httpx, "AsyncClient", _ManifestClient):
        result = _run(mangadex_.sync_mangadex_chapters(7))

    assert result == {"added": 1, "updated": 0, "total": 1, "external_skipped": 0}
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        manifest_ids = [
            row[0]
            for row in db.execute(
                "SELECT mangadex_chapter_id FROM mangadex_chapters WHERE series_id=7"
            )
        ]
        chapter = db.execute(
            "SELECT title,pages,metadata_source,metadata_updated_at"
            " FROM chapters WHERE series_id=7 AND chapter_num=1"
        ).fetchone()
        source = db.execute(
            "SELECT status FROM series_metadata_sources"
            " WHERE series_id=7 AND source='mangadex_manifest'"
        ).fetchone()
    assert manifest_ids == ["current"]
    assert chapter["title"] == "The Beginning"
    assert chapter["pages"] == 24
    assert chapter["metadata_source"] == "mangadex"
    assert chapter["metadata_updated_at"]
    assert source["status"] == "healthy"


class _RateLimitedResponse:
    status_code = 429
    headers = {"X-RateLimit-Retry-After": "0"}

    def raise_for_status(self):
        import httpx

        request = httpx.Request("GET", "https://api.mangadex.org/feed")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)


class _RateLimitedClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return _RateLimitedResponse()


def test_manifest_rate_limit_preserves_cached_rows(db_path):
    from routers import mangadex_

    _seed_series(db_path, mangadex_id="mdx-1", ddl_language="en")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO mangadex_chapters"
            " (series_id,mangadex_chapter_id,chapter_num,language)"
            " VALUES(7,'cached',1,'en')"
        )

    with patch.object(mangadex_.httpx, "AsyncClient", _RateLimitedClient):
        with pytest.raises(Exception, match="rate limited"):
            _run(mangadex_.sync_mangadex_chapters(7))

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        cached = db.execute(
            "SELECT mangadex_chapter_id FROM mangadex_chapters WHERE series_id=7"
        ).fetchall()
        source = db.execute(
            "SELECT status,failure_count FROM series_metadata_sources"
            " WHERE series_id=7 AND source='mangadex_manifest'"
        ).fetchone()
    assert [row["mangadex_chapter_id"] for row in cached] == ["cached"]
    assert source["status"] == "failed"
    assert source["failure_count"] == 1


def test_reinject_metadata_deduplicates_shared_archives(db_path):
    import main
    from routers.series_ import reinject_metadata

    _seed_series(db_path)
    with sqlite3.connect(db_path) as db:
        volume_id = db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,import_path)"
            " VALUES(7,1,'downloaded','/library/shared.cbz')"
        ).lastrowid
        db.executemany(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,import_path)"
            " VALUES(7,?,?, 'downloaded',?)",
            [
                (volume_id, 1, "/library/shared.cbz"),
                (None, 2, "/library/chapter-2.cbz"),
            ],
        )

    writes = []

    def _build(_series, *, volume_num=None, chapter_num=None, tags=None):
        return f"volume={volume_num};chapter={chapter_num}"

    def _inject(path, xml):
        writes.append((path, xml))
        return True

    with (
        patch.object(main, "build_comicinfo_xml", _build),
        patch.object(main, "inject_comicinfo", _inject),
        patch("routers.series_.os.path.isfile", return_value=True),
    ):
        response = _run(reinject_metadata(7))

    assert response.status_code == 200
    assert writes == [
        ("/library/shared.cbz", "volume=1.0;chapter=None"),
        ("/library/chapter-2.cbz", "volume=None;chapter=2.0"),
    ]
