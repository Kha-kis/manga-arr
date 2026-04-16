"""Hermetic tests for app/routers/suwayomi_.py.

Suwayomi exposes GraphQL. Production code touches it through `_gql(c, ...)`.
Tests mock httpx.AsyncClient.post() to return canned GraphQL payloads.

Coverage focus (high value, small surface):
  - pure helpers: classify_source, _titles_match, _best_title_match,
    _vol_from_name, _ch_sort_key, _normalise_dir_name
  - _gql success and GraphQL-error path (raises RuntimeError with the message)
  - _gql HTTP error path
  - test_connection success and failure shapes
  - get_source_id with cache hit + miss + language fallback

No live Suwayomi contact.
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


@pytest.fixture
def fresh_db():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-swy-keys-")

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


# ─────────────────────── pure helpers ────────────────────────────────────────

def test_classify_source_official():
    from routers.suwayomi_ import classify_source
    assert classify_source("Manga Plus") == "official"
    assert classify_source("MANGAPLUS")  == "official"
    assert classify_source("Viz Media")  == "official"


def test_classify_source_aggregator():
    from routers.suwayomi_ import classify_source
    assert classify_source("MangaDex (en)") == "aggregator"


def test_classify_source_falls_back_to_fan():
    from routers.suwayomi_ import classify_source
    assert classify_source("RandomFanScans") == "fan"


def test_titles_match_handles_punctuation_and_case():
    from routers.suwayomi_ import _titles_match
    assert _titles_match("Vinland Saga", "vinland saga")
    assert _titles_match("One Piece!", "One   Piece")
    assert _titles_match("Berserk: Black Swordsman", "Berserk Black Swordsman")
    assert not _titles_match("Naruto", "Bleach")


def test_best_title_match_picks_exact_match_first():
    from routers.suwayomi_ import _best_title_match
    results = [
        {"id": 10, "title": "Vinland Saga: Side Story"},
        {"id": 20, "title": "Vinland Saga"},
        {"id": 30, "title": "Vinland"},
    ]
    assert _best_title_match(results, "Vinland Saga") == 20


def test_best_title_match_falls_back_to_solo_result():
    from routers.suwayomi_ import _best_title_match
    assert _best_title_match([{"id": 99, "title": "Almost"}], "Different") == 99


def test_best_title_match_returns_none_when_ambiguous():
    """Multiple results, none exact → caller decides; return None."""
    from routers.suwayomi_ import _best_title_match
    results = [
        {"id": 1, "title": "One"},
        {"id": 2, "title": "Two"},
    ]
    assert _best_title_match(results, "Three") is None


def test_best_title_match_returns_none_for_empty_list():
    from routers.suwayomi_ import _best_title_match
    assert _best_title_match([], "anything") is None


def test_vol_from_name_parses_standard_format():
    from routers.suwayomi_ import _vol_from_name
    assert _vol_from_name("Vol.1 Ch.2 - Prologue") == 1.0
    assert _vol_from_name("vol.10 ch.99") == 10.0
    assert _vol_from_name("Ch.5") is None
    assert _vol_from_name(None) is None
    assert _vol_from_name("") is None


def test_vol_from_name_handles_decimal_volume():
    from routers.suwayomi_ import _vol_from_name
    assert _vol_from_name("Vol.1.5 Special") == 1.5


# ─────────────────────── _gql wrapper ────────────────────────────────────────

class _MockResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}
    def json(self):
        return self._json
    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            req = httpx.Request("POST", "http://test")
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError("err", request=req, response=resp)


def _make_async_client(post_response):
    """Returns a stand-in AsyncClient whose .post() returns `post_response`."""
    captured: dict = {}
    class _C:
        def __init__(self, *a, **kw):
            captured.setdefault("init_kwargs", []).append(kw)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, **kw):
            captured.setdefault("posts", []).append({"url": url, "json": json, "kw": kw})
            return post_response
    return _C, captured


def test_gql_returns_data_on_success():
    from routers.suwayomi_ import _gql
    cli_class, captured = _make_async_client(
        _MockResponse(200, {"data": {"sources": {"nodes": [{"id": "1", "name": "Foo", "lang": "en"}]}}})
    )
    with patch("httpx.AsyncClient", new=cli_class):
        data = asyncio.run(_gql(
            {"host": "http://swy.local", "username": "", "password": ""},
            "{ sources { nodes { id name lang } } }"
        ))
    assert data == {"sources": {"nodes": [{"id": "1", "name": "Foo", "lang": "en"}]}}
    posts = captured["posts"]
    assert posts[0]["url"] == "http://swy.local/api/graphql"
    assert "query" in posts[0]["json"]


def test_gql_raises_runtime_error_on_graphql_errors():
    """GraphQL responses with `errors` must raise, joining messages — not silently
    return None."""
    from routers.suwayomi_ import _gql
    cli_class, _ = _make_async_client(
        _MockResponse(200, {"errors": [{"message": "Source not found"}, {"message": "boom"}]})
    )
    with patch("httpx.AsyncClient", new=cli_class), \
         pytest.raises(RuntimeError) as exc_info:
        asyncio.run(_gql({"host": "http://swy.local", "username": "", "password": ""}, "{}"))
    assert "Source not found" in str(exc_info.value)
    assert "boom" in str(exc_info.value)


def test_gql_raises_on_http_error_status():
    from routers.suwayomi_ import _gql
    import httpx
    cli_class, _ = _make_async_client(_MockResponse(500))
    with patch("httpx.AsyncClient", new=cli_class), \
         pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_gql({"host": "http://swy.local", "username": "", "password": ""}, "{}"))


def test_gql_returns_empty_dict_when_data_field_missing():
    """payload with neither errors nor data → returns {} (no KeyError)."""
    from routers.suwayomi_ import _gql
    cli_class, _ = _make_async_client(_MockResponse(200, {}))
    with patch("httpx.AsyncClient", new=cli_class):
        data = asyncio.run(_gql({"host": "http://swy.local", "username": "", "password": ""}, "{}"))
    assert data == {}


# ─────────────────────── test_connection ─────────────────────────────────────

def test_connection_success_reports_source_count():
    from routers.suwayomi_ import test_connection
    cli_class, _ = _make_async_client(_MockResponse(200, {
        "data": {"sources": {"nodes": [
            {"id": "1", "name": "MangaDex (en)", "lang": "en"},
            {"id": "2", "name": "Manga Plus", "lang": "en"},
            {"id": "3", "name": "FanScans", "lang": "en"},
        ]}}
    }))
    with patch("httpx.AsyncClient", new=cli_class):
        ok, msg = asyncio.run(test_connection({"host": "http://swy.local",
                                                "username": "", "password": ""}))
    assert ok is True
    assert "3 sources" in msg
    # Manga Plus is in KNOWN_OFFICIAL set
    assert "1 official" in msg


def test_connection_returns_false_on_error():
    from routers.suwayomi_ import test_connection
    cli_class, _ = _make_async_client(_MockResponse(500))
    with patch("httpx.AsyncClient", new=cli_class):
        ok, msg = asyncio.run(test_connection({"host": "http://swy.local",
                                                "username": "", "password": ""}))
    assert ok is False
    assert msg  # has a non-empty error description


# ─────────────────────── get_source_id + cache ───────────────────────────────

def test_get_source_id_finds_exact_lang_match():
    import routers.suwayomi_ as swy
    swy._SOURCE_CACHE.clear()
    cli_class, _ = _make_async_client(_MockResponse(200, {
        "data": {"sources": {"nodes": [
            {"id": "11", "name": "MangaDex", "lang": "en"},
            {"id": "12", "name": "MangaDex", "lang": "fr"},
        ]}}
    }))
    with patch("httpx.AsyncClient", new=cli_class):
        sid = asyncio.run(swy.get_source_id(
            {"host": "http://swy.local", "username": "", "password": ""},
            "mangadex", "fr"))
    assert sid == "12"


def test_get_source_id_falls_back_to_english():
    """Requested language has no match → fall back to English variant."""
    import routers.suwayomi_ as swy
    swy._SOURCE_CACHE.clear()
    cli_class, _ = _make_async_client(_MockResponse(200, {
        "data": {"sources": {"nodes": [
            {"id": "21", "name": "MangaDex", "lang": "en"},
        ]}}
    }))
    with patch("httpx.AsyncClient", new=cli_class):
        sid = asyncio.run(swy.get_source_id(
            {"host": "http://swy.local", "username": "", "password": ""},
            "mangadex", "ja"))
    assert sid == "21"


def test_get_source_id_returns_none_when_no_match():
    import routers.suwayomi_ as swy
    swy._SOURCE_CACHE.clear()
    cli_class, _ = _make_async_client(_MockResponse(200, {
        "data": {"sources": {"nodes": [
            {"id": "30", "name": "FanScans", "lang": "en"},
        ]}}
    }))
    with patch("httpx.AsyncClient", new=cli_class):
        sid = asyncio.run(swy.get_source_id(
            {"host": "http://swy.local", "username": "", "password": ""},
            "mangadex", "en"))
    assert sid is None


def test_get_source_id_uses_cache_on_second_call():
    """A second lookup for the same key must not call the upstream again."""
    import routers.suwayomi_ as swy
    swy._SOURCE_CACHE.clear()
    cli_class, captured = _make_async_client(_MockResponse(200, {
        "data": {"sources": {"nodes": [
            {"id": "41", "name": "MangaDex", "lang": "en"},
        ]}}
    }))
    with patch("httpx.AsyncClient", new=cli_class):
        first = asyncio.run(swy.get_source_id(
            {"host": "http://swy.local", "username": "", "password": ""},
            "mangadex", "en"))
        second = asyncio.run(swy.get_source_id(
            {"host": "http://swy.local", "username": "", "password": ""},
            "mangadex", "en"))
    assert first == second == "41"
    # First call hit the upstream; second was cached → at most 1 POST.
    assert len(captured["posts"]) == 1


# ─────────────────────── _chapters_for_volume ────────────────────────────────

def test_chapters_for_volume_parses_vol_prefix(fresh_db):
    """Primary path: extract Vol.X from chapter name."""
    from routers.suwayomi_ import _chapters_for_volume
    chapters = [
        {"id": 1, "name": "Vol.1 Ch.1 - Start"},
        {"id": 2, "name": "Vol.1 Ch.2 - More"},
        {"id": 3, "name": "Vol.2 Ch.3 - Later"},
        {"id": 4, "name": "Ch.4 (no vol)"},
    ]
    matched = _chapters_for_volume(chapters, 1.0)
    assert {ch["id"] for ch in matched} == {1, 2}


def test_chapters_for_volume_returns_empty_when_no_match(fresh_db):
    from routers.suwayomi_ import _chapters_for_volume
    chapters = [{"id": 1, "name": "Vol.5 Ch.1"}]
    matched = _chapters_for_volume(chapters, 99.0)
    assert matched == []
