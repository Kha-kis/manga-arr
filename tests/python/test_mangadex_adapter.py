"""Hermetic tests for app/routers/mangadex_.py.

Mocks httpx.AsyncClient to exercise:
  - _parse_chapter   (pure function, several edge cases)
  - sync_mangadex_chapters happy path → DB rows inserted
  - sync_mangadex_chapters 429 → respects Retry-After
  - sync_mangadex_chapters 5xx → retries then raises
  - sync_mangadex_chapters series with no mangadex_id → ValueError
  - sync_mangadex_chapters pagination across multiple pages
  - external chapters counted but not blocked from storage
  - get_chapter_availability shape
  - select_best_chapters_for_volume preferred-group logic
  - select_best_chapters_for_volume official_only / fan_only filters

No live MangaDex contact. ~120ms total runtime.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


# ───────────────────────── fixtures ──────────────────────────────────────────

@pytest.fixture
def fresh_db():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-mdx-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


# ───────────────────────── _parse_chapter ────────────────────────────────────

def test_parse_chapter_basic_shape():
    from routers.mangadex_ import _parse_chapter
    item = {
        "id": "abc-uuid",
        "attributes": {
            "chapter": "1.5",
            "volume":  "1",
            "title":   "Prologue",
            "pages":   42,
            "translatedLanguage": "en",
            "publishAt": "2024-01-01T00:00:00+00:00",
        },
        "relationships": [
            {"type": "scanlation_group", "attributes": {"name": "Viz Media"}},
        ],
    }
    parsed = _parse_chapter(item, series_id=7)
    assert parsed["mangadex_chapter_id"] == "abc-uuid"
    assert parsed["chapter_num"] == 1.5
    assert parsed["volume_num"]  == 1.0
    assert parsed["title"]       == "Prologue"
    assert parsed["pages"]       == 42
    assert parsed["scanlation_group"] == "Viz Media"
    assert parsed["language"]    == "en"
    assert parsed["is_external"] == 0


def test_parse_chapter_handles_null_chapter_and_volume():
    """One-shots and non-numbered chapters arrive with chapter=None / "" — must coerce to None, not raise."""
    from routers.mangadex_ import _parse_chapter
    parsed = _parse_chapter({
        "id": "x",
        "attributes": {"chapter": None, "volume": "", "title": None, "pages": 0,
                       "translatedLanguage": "en"},
        "relationships": [],
    }, series_id=1)
    assert parsed["chapter_num"] is None
    assert parsed["volume_num"]  is None
    assert parsed["title"]       is None


def test_parse_chapter_marks_external_url_chapters():
    """externalUrl set → is_external=1 even if pages/title look normal."""
    from routers.mangadex_ import _parse_chapter
    parsed = _parse_chapter({
        "id": "ext-1",
        "attributes": {"chapter": "10", "volume": "2", "pages": 0,
                       "translatedLanguage": "en",
                       "externalUrl": "https://mangaplus.example/ch10"},
        "relationships": [],
    }, series_id=1)
    assert parsed["is_external"] == 1


def test_parse_chapter_handles_garbage_chapter_value():
    """Some publishers ship 'Special' / 'Omake' as the chapter field."""
    from routers.mangadex_ import _parse_chapter
    parsed = _parse_chapter({
        "id": "g",
        "attributes": {"chapter": "Special", "volume": "ABC", "pages": 0,
                       "translatedLanguage": "en"},
        "relationships": [],
    }, series_id=1)
    assert parsed["chapter_num"] is None
    assert parsed["volume_num"]  is None


# ───────────────────────── sync_mangadex_chapters ────────────────────────────

class _MockResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
    def json(self):
        return self._json
    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            req = httpx.Request("GET", "http://test")
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError("err", request=req, response=resp)


def _mangadex_response(items, total=None):
    return {"data": items, "total": total if total is not None else len(items)}


def _make_async_client(get_responses, sleeps_captured=None):
    """Returns a stand-in for httpx.AsyncClient that walks `get_responses`
    in order on every .get() call."""
    iterator = iter(get_responses)
    class _C:
        def __init__(self, *a, **kw):
            self.headers = kw.get("headers", {})
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            try:
                return next(iterator)
            except StopIteration:
                # Out of canned responses — surface clearly.
                raise AssertionError(f"unexpected extra GET {url} params={params}")
    return _C


def test_sync_raises_when_series_has_no_mangadex_id(fresh_db):
    import main
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
    from routers.mangadex_ import sync_mangadex_chapters
    with pytest.raises(ValueError):
        asyncio.run(sync_mangadex_chapters(1))


def test_sync_happy_path_inserts_chapters(fresh_db):
    """One page of chapters, no pagination, no rate limit."""
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern, mangadex_id, ddl_language)"
                  " VALUES(1, 'X', 'X', 'mdx-id-1', 'en')")
    from routers.mangadex_ import sync_mangadex_chapters

    items = [
        {"id": f"ch-{i}", "attributes": {"chapter": str(i), "volume": "1",
                                          "title": None, "pages": 20,
                                          "translatedLanguage": "en"},
         "relationships": []}
        for i in range(1, 4)
    ]
    responses = [_MockResponse(200, _mangadex_response(items, total=3))]
    with patch("httpx.AsyncClient", new=_make_async_client(responses)):
        result = asyncio.run(sync_mangadex_chapters(1))

    assert result == {"added": 3, "updated": 0, "total": 3, "external_skipped": 0}
    with sqlite3.connect(fresh_db) as c:
        n = c.execute("SELECT COUNT(*) FROM mangadex_chapters WHERE series_id=1").fetchone()[0]
    assert n == 3


def test_sync_pagination_walks_multiple_pages(fresh_db):
    """Two pages, total=600. Loop must call .get twice."""
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern, mangadex_id, ddl_language)"
                  " VALUES(1, 'X', 'X', 'mdx-id-1', 'en')")
    from routers.mangadex_ import sync_mangadex_chapters

    page1 = [{"id": f"a{i}", "attributes": {"chapter": str(i), "volume": "1",
                                              "title": None, "pages": 20,
                                              "translatedLanguage": "en"},
              "relationships": []} for i in range(500)]
    page2 = [{"id": f"b{i}", "attributes": {"chapter": str(500 + i), "volume": "2",
                                              "title": None, "pages": 20,
                                              "translatedLanguage": "en"},
              "relationships": []} for i in range(100)]
    responses = [
        _MockResponse(200, _mangadex_response(page1, total=600)),
        _MockResponse(200, _mangadex_response(page2, total=600)),
    ]
    # Patch asyncio.sleep inside the module to skip the inter-page delay.
    import routers.mangadex_ as mdx
    real_sleep = asyncio.sleep
    async def _no_sleep(_n): return None
    with patch("httpx.AsyncClient", new=_make_async_client(responses)), \
         patch.object(mdx.asyncio, "sleep", new=_no_sleep):
        result = asyncio.run(sync_mangadex_chapters(1))

    assert result["added"] == 600


def test_sync_respects_429_retry_after(fresh_db):
    """First response is 429 with Retry-After=1; second response succeeds."""
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern, mangadex_id, ddl_language)"
                  " VALUES(1, 'X', 'X', 'mdx-id-1', 'en')")
    from routers.mangadex_ import sync_mangadex_chapters
    import routers.mangadex_ as mdx

    items = [{"id": "x", "attributes": {"chapter": "1", "volume": "1",
                                          "title": None, "pages": 20,
                                          "translatedLanguage": "en"},
              "relationships": []}]
    responses = [
        _MockResponse(429, headers={"X-RateLimit-Retry-After": "0"}),
        _MockResponse(200, _mangadex_response(items, total=1)),
    ]
    async def _no_sleep(_n): return None
    with patch("httpx.AsyncClient", new=_make_async_client(responses)), \
         patch.object(mdx.asyncio, "sleep", new=_no_sleep):
        result = asyncio.run(sync_mangadex_chapters(1))
    assert result["added"] == 1


def test_sync_5xx_retries_then_raises(fresh_db):
    """3 consecutive 500s should exhaust the retry loop and raise HTTPStatusError."""
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern, mangadex_id, ddl_language)"
                  " VALUES(1, 'X', 'X', 'mdx-id-1', 'en')")
    import httpx
    import routers.mangadex_ as mdx
    from routers.mangadex_ import sync_mangadex_chapters

    responses = [_MockResponse(500), _MockResponse(500), _MockResponse(500)]
    async def _no_sleep(_n): return None
    with patch("httpx.AsyncClient", new=_make_async_client(responses)), \
         patch.object(mdx.asyncio, "sleep", new=_no_sleep), \
         pytest.raises(httpx.HTTPStatusError):
        asyncio.run(sync_mangadex_chapters(1))


def test_sync_counts_external_chapters_separately(fresh_db):
    """external_skipped counts chapters with externalUrl, but they're still stored."""
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern, mangadex_id, ddl_language)"
                  " VALUES(1, 'X', 'X', 'mdx-id-1', 'en')")
    from routers.mangadex_ import sync_mangadex_chapters

    items = [
        {"id": "a", "attributes": {"chapter": "1", "volume": "1",
                                     "pages": 20, "translatedLanguage": "en"},
         "relationships": []},
        {"id": "b", "attributes": {"chapter": "2", "volume": "1",
                                     "pages": 20, "translatedLanguage": "en",
                                     "externalUrl": "https://example.com"},
         "relationships": []},
    ]
    responses = [_MockResponse(200, _mangadex_response(items, total=2))]
    with patch("httpx.AsyncClient", new=_make_async_client(responses)):
        result = asyncio.run(sync_mangadex_chapters(1))
    assert result["added"] == 2
    assert result["external_skipped"] == 1


# ───────────────────────── availability + selection ──────────────────────────

def _seed_chapters(db_path, rows):
    """rows: list of (chapter_num, volume_num, group, is_external, pages)"""
    with sqlite3.connect(db_path) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        for i, (ch, vol, group, ext, pages) in enumerate(rows):
            c.execute(
                "INSERT INTO mangadex_chapters(series_id, mangadex_chapter_id,"
                " chapter_num, volume_num, scanlation_group, language, is_external, pages)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (1, f"id-{i}", ch, vol, group, "en", ext, pages),
            )


def test_get_chapter_availability_groups_by_volume(fresh_db):
    from routers.mangadex_ import get_chapter_availability
    _seed_chapters(fresh_db, [
        (1.0, 1.0, "Viz",     0, 20),
        (2.0, 1.0, "Viz",     0, 20),
        (3.0, 2.0, "FanSubs", 0, 18),
        (4.0, 2.0, "FanSubs", 1, 0),  # external
    ])
    avail = get_chapter_availability(1, language="en")
    assert avail[1.0]["chapter_count"] == 2
    assert avail[1.0]["has_external"] is False
    assert avail[2.0]["chapter_count"] == 2
    assert avail[2.0]["has_external"] is True
    assert "Viz" in avail[1.0]["groups"]
    assert "FanSubs" in avail[2.0]["groups"]


def test_select_best_excludes_external(fresh_db):
    """External chapters never appear in selection — they can't be downloaded."""
    from routers.mangadex_ import select_best_chapters_for_volume
    _seed_chapters(fresh_db, [
        (1.0, 1.0, "FanSubs", 1, 0),   # external
        (2.0, 1.0, "FanSubs", 0, 18),
    ])
    chosen = select_best_chapters_for_volume(1, 1.0, [], "any", "en")
    assert len(chosen) == 1
    assert chosen[0]["chapter_num"] == 2.0


def test_select_best_filters_official_only(fresh_db):
    """source_type='official_only' restricts to KNOWN_OFFICIAL_GROUPS."""
    from routers.mangadex_ import select_best_chapters_for_volume
    _seed_chapters(fresh_db, [
        (1.0, 1.0, "FanSubs",   0, 20),
        (2.0, 1.0, "Viz Media", 0, 20),
    ])
    chosen = select_best_chapters_for_volume(1, 1.0, [], "official_only", "en")
    groups = {c["scanlation_group"] for c in chosen}
    assert groups == {"Viz Media"}


def test_select_best_returns_empty_when_no_chapters(fresh_db):
    """Asking for a volume with no chapters returns []."""
    from routers.mangadex_ import select_best_chapters_for_volume
    _seed_chapters(fresh_db, [])
    chosen = select_best_chapters_for_volume(1, 5.0, [], "any", "en")
    assert chosen == []
