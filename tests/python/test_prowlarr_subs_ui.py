"""HTTP-level tests for the Prowlarr sub-indexer visibility endpoint.

The /indexers/{id}/prowlarr-subs endpoint returns a rendered HTML
partial with the live sub-indexer list, so operators can see inline
which Prowlarr sub-indexers Mangarr is actually polling — without
bouncing to Prowlarr's own UI to check.

Tests cover:
  - Happy path: HTMX call returns rendered list with correct status
    badges (POLLED / DISABLED IN PROWLARR / NO MANGA CAPS)
  - Sort order: polled-first, then alpha
  - Error paths: unknown indexer (404), non-Prowlarr type, missing URL
  - The underlying _list_prowlarr_subs_for_ui helper preserves disabled
    entries (in contrast to _get_prowlarr_indexers, which filters them)
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB + a Prowlarr indexer + a torznab indexer."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-prowlarr-ui-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled, categories)"
            " VALUES(101, 'TestProwlarr', 'prowlarr', 'http://prowlarr.test', 'fake-key',"
            "        1, '[7000,7010,7020]')"
        )
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled, categories)"
            " VALUES(102, 'TestTorznab', 'torznab', 'http://nyaa.test', 'fake-key',"
            "        1, '[7000]')"
        )
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled, categories)"
            " VALUES(103, 'NoUrlProwlarr', 'prowlarr', '', '',"
            "        1, '[7000,7010,7020]')"
        )

    try:
        yield {'db_path': db.name}
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _client():
    import main
    from fastapi.testclient import TestClient
    return TestClient(main.app)


class _MockResp:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


def _mock_prowlarr(indexers_response):
    class _C:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None):
            return _MockResp(200, indexers_response)
    return _C


# ───────────────────── happy-path rendering ─────────────────────


def test_endpoint_renders_subs_with_correct_status_badges(env):
    """The headline test: three sub-indexers (one polled, one disabled,
    one missing manga caps) → rendered partial shows the right badge for
    each."""
    fake_subs = [
        {
            'id': 1, 'name': 'Nyaa', 'enable': True, 'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 7000}, {'id': 7020}]},
        },
        {
            'id': 2, 'name': 'OldDisabledIndexer', 'enable': False, 'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 7000}]},
        },
        {
            'id': 3, 'name': 'MoviesOnly', 'enable': True, 'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 2000}, {'id': 2010}]},
        },
    ]

    client = _client()

    with patch('httpx.AsyncClient', new=_mock_prowlarr(fake_subs)):
        r = client.get("/indexers/101/prowlarr-subs")

    assert r.status_code == 200, r.text
    body = r.text

    # All three indexer names rendered
    assert 'Nyaa' in body
    assert 'OldDisabledIndexer' in body
    assert 'MoviesOnly' in body

    # Status badges
    assert 'POLLED' in body, "active indexer must carry POLLED badge"
    assert 'DISABLED IN PROWLARR' in body, (
        "Prowlarr-disabled indexer must carry DISABLED IN PROWLARR badge"
    )
    assert 'NO MANGA CAPS' in body, (
        "indexer without 7000-series categories must carry NO MANGA CAPS badge"
    )

    # Counter line shows '1 of 3 active'
    assert '1 of 3 active' in body, (
        f"counter line should report 1-of-3 active for manga; body excerpt:\n{body[:500]}"
    )


def test_endpoint_sorts_polled_first(env):
    """Polled sub-indexers come before non-polled in the rendered output —
    operators care about active ones first."""
    fake_subs = [
        {
            'id': 1, 'name': 'AlphaDisabled', 'enable': False, 'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 7000}]},
        },
        {
            'id': 2, 'name': 'BetaPolled', 'enable': True, 'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 7000}]},
        },
    ]

    client = _client()
    with patch('httpx.AsyncClient', new=_mock_prowlarr(fake_subs)):
        r = client.get("/indexers/101/prowlarr-subs")

    body = r.text
    # BetaPolled (active) must appear before AlphaDisabled (despite alpha order)
    assert body.find('BetaPolled') < body.find('AlphaDisabled'), (
        "polled sub-indexers must be sorted before non-polled ones"
    )


# ───────────────────── error paths ─────────────────────


def test_endpoint_returns_404_for_unknown_indexer(env):
    client = _client()
    r = client.get("/indexers/99999/prowlarr-subs")
    assert r.status_code == 404
    assert 'not found' in r.text.lower()


def test_endpoint_rejects_non_prowlarr_indexer(env):
    """Calling the endpoint with a torznab-type indexer ID must return a
    clear message rather than try to call /api/v1/indexer (which only
    Prowlarr exposes)."""
    client = _client()
    r = client.get("/indexers/102/prowlarr-subs")
    assert r.status_code == 200, r.text
    body = r.text.lower()
    assert 'only available for prowlarr' in body
    assert 'torznab' in body, "error message should name the actual type"


def test_endpoint_handles_missing_url_gracefully(env):
    """Prowlarr indexer with empty URL: error partial, no httpx call attempted."""
    client = _client()
    r = client.get("/indexers/103/prowlarr-subs")
    assert r.status_code == 200, r.text
    assert 'no url configured' in r.text.lower()


def test_endpoint_handles_prowlarr_failure(env):
    """If Prowlarr returns non-200 or the request raises, the helper
    returns an empty list — the partial shows the 'no sub-indexers' note
    instead of crashing."""
    class _FailingClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None):
            raise RuntimeError("connection refused")

    client = _client()
    with patch('httpx.AsyncClient', new=_FailingClient):
        r = client.get("/indexers/101/prowlarr-subs")
    assert r.status_code == 200
    assert 'no sub-indexers' in r.text.lower()


# ───────────────────── auth regression ─────────────────────


def test_endpoint_does_not_require_api_key(env):
    """Regression: this endpoint is hit by HTMX from an authenticated
    BROWSER session, not a programmatic API client. The browser doesn't
    send X-Api-Key. If the endpoint moves back under /api/ (the original
    location, fixed in #116), HTMX calls would 401 silently — the user
    would click the binoculars button and see nothing happen.

    Asserts a plain GET with no auth headers and no cookies returns 200
    (the response body itself depends on whether Prowlarr is reachable;
    we just assert the auth layer didn't reject the request)."""
    fake_subs = [{
        'id': 1, 'name': 'AnyIndexer', 'enable': True, 'protocol': 'torrent',
        'capabilities': {'categories': [{'id': 7000}]},
    }]

    client = _client()
    with patch('httpx.AsyncClient', new=_mock_prowlarr(fake_subs)):
        # No headers, no cookies — exactly what an HTMX request from a
        # browser would look like at the auth layer (HTMX does inject
        # csrftoken on POSTs but this is a GET).
        r = client.get("/indexers/101/prowlarr-subs")

    assert r.status_code == 200, (
        f"endpoint must work without X-Api-Key — moving it back to /api/* "
        f"would re-introduce the silent 401 bug from #115. Got {r.status_code}: "
        f"{r.text[:200]!r}"
    )
    # Verify the response actually rendered the partial (not just a 200 from
    # something else like a redirect).
    assert 'AnyIndexer' in r.text


# ───────────────────── helper isolation ─────────────────────


def test_helper_preserves_disabled_subs():
    """Critical contract: _list_prowlarr_subs_for_ui must return ALL
    Prowlarr sub-indexers (including disabled ones) so the UI can show
    the user why each is or isn't being polled. _get_prowlarr_indexers
    filters; this helper does NOT."""
    from routers.indexers import _list_prowlarr_subs_for_ui

    fake_subs = [
        {
            'id': 1, 'name': 'Active', 'enable': True, 'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 7000}]},
        },
        {
            'id': 2, 'name': 'Disabled', 'enable': False, 'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 7000}]},
        },
        {
            'id': 3, 'name': 'NoMangaCaps', 'enable': True, 'protocol': 'torrent',
            'capabilities': {'categories': [{'id': 2000}]},
        },
    ]

    with patch('httpx.AsyncClient', new=_mock_prowlarr(fake_subs)):
        result = asyncio.run(
            _list_prowlarr_subs_for_ui('http://test', 'k', [7000, 7010, 7020])
        )

    names = [r['name'] for r in result]
    assert set(names) == {'Active', 'Disabled', 'NoMangaCaps'}, (
        f"helper must preserve all sub-indexers including disabled ones; got {names!r}"
    )

    by_name = {r['name']: r for r in result}
    assert by_name['Active']['will_be_polled'] is True
    assert by_name['Disabled']['will_be_polled'] is False
    assert by_name['Disabled']['enable'] is False
    assert by_name['NoMangaCaps']['will_be_polled'] is False
    assert by_name['NoMangaCaps']['manga_compatible'] is False
