"""HTTP-level tests for the Prowlarr sub-import flow (Sonarr/Radarr-style).

Two endpoints under test:
  GET  /indexers/{id}/sync-prowlarr-preview  → modal partial with checkboxes
  POST /indexers/{id}/sync-prowlarr           → commits selected subs as torznab rows

The headline behavior:
  - Each imported sub becomes its own type='torznab' indexer row pointing at
    Prowlarr's per-indexer torznab façade (<base>/<sub-id>/api).
  - parent_prowlarr_id + prowlarr_indexer_id form the dedup key on re-sync.
  - The user can then enable/disable, prioritize, and tune each row independently
    via the regular /indexers UI.
"""
import asyncio
import json as _json
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
    key_dir = tempfile.mkdtemp(prefix="mangarr-sync-keys-")

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
            "INSERT INTO indexers(id, name, type, url, api_key, enabled,"
            " categories, priority, min_seeders, seed_ratio)"
            " VALUES(101, 'TestProwlarr', 'prowlarr', 'http://prowlarr.test', 'fake-key',"
            "        1, '[7000,7010,7020]', 5, 3, 1.5)"
        )
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled, categories)"
            " VALUES(102, 'TestTorznab', 'torznab', 'http://nyaa.test', 'fake-key',"
            "        1, '[7000]')"
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


def _csrf(tag="sync"):
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


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


_FAKE_PROWLARR_RESPONSE = [
    {
        'id': 1, 'name': 'Nyaa', 'enable': True, 'protocol': 'torrent',
        'capabilities': {'categories': [{'id': 7000}, {'id': 7020}]},
    },
    {
        'id': 2, 'name': 'AnimeBytes', 'enable': True, 'protocol': 'torrent',
        'capabilities': {'categories': [{'id': 7000}, {'id': 7010}]},
    },
    {
        'id': 3, 'name': 'OldDisabledIndexer', 'enable': False, 'protocol': 'torrent',
        'capabilities': {'categories': [{'id': 7000}]},
    },
    {
        'id': 4, 'name': 'MoviesOnly', 'enable': True, 'protocol': 'torrent',
        'capabilities': {'categories': [{'id': 2000}, {'id': 2010}]},
    },
]


# ───────────────────── preview (modal) ─────────────────────


def test_preview_renders_eligible_subs_with_checkboxes(env):
    client = _client()
    with patch('httpx.AsyncClient', new=_mock_prowlarr(_FAKE_PROWLARR_RESPONSE)):
        r = client.get("/indexers/101/sync-prowlarr-preview")
    assert r.status_code == 200, r.text
    body = r.text
    # All 4 subs must appear (so user sees what's available)
    assert 'Nyaa' in body
    assert 'AnimeBytes' in body
    assert 'OldDisabledIndexer' in body
    assert 'MoviesOnly' in body
    # Eligible ones get checkbox checked by default
    assert 'value="1"' in body and 'value="2"' in body
    # Ineligible ones are disabled (can't be checked even if user tries)
    assert 'DISABLED IN PROWLARR' in body
    assert 'NO MANGA CAPS' in body
    # Form points at the correct submit URL
    assert 'action="/indexers/101/sync-prowlarr"' in body


def test_preview_marks_already_imported_subs(env):
    """On re-open, subs already imported in a prior sync are flagged so the
    user doesn't accidentally try to re-import them."""
    with sqlite3.connect(env['db_path']) as c:
        # Pre-seed: AnimeBytes (sub-id 2) was imported in a prior session
        c.execute(
            "INSERT INTO indexers(name, type, url, api_key, enabled, categories,"
            " parent_prowlarr_id, prowlarr_indexer_id)"
            " VALUES('AnimeBytes', 'torznab', 'http://prowlarr.test/2', 'fake-key',"
            "        1, '[7000,7010]', 101, 2)"
        )

    client = _client()
    with patch('httpx.AsyncClient', new=_mock_prowlarr(_FAKE_PROWLARR_RESPONSE)):
        r = client.get("/indexers/101/sync-prowlarr-preview")
    assert r.status_code == 200, r.text
    body = r.text
    assert 'ALREADY IMPORTED' in body, "previously imported sub must carry that badge"


def test_preview_rejects_non_prowlarr_indexer(env):
    client = _client()
    r = client.get("/indexers/102/sync-prowlarr-preview")
    assert r.status_code == 400, r.text
    assert 'only available for prowlarr' in r.text.lower()


def test_preview_returns_404_for_unknown_indexer(env):
    client = _client()
    r = client.get("/indexers/99999/sync-prowlarr-preview")
    assert r.status_code == 404


# ───────────────────── commit (POST) ─────────────────────


def test_sync_creates_torznab_rows_for_selected_subs(env):
    """Headline test: post selected sub-ids → new torznab rows materialize
    with correct URL pattern, copied API key, correct attribution."""
    client = _client()
    csrf = _csrf("sync-create")

    with patch('httpx.AsyncClient', new=_mock_prowlarr(_FAKE_PROWLARR_RESPONSE)):
        r = client.post(
            "/indexers/101/sync-prowlarr",
            data={
                'csrf_token': csrf['headers']['X-CSRFToken'],
                'selected': ['1', '2'],
            },
            **csrf,
            follow_redirects=False,
        )
    assert r.status_code == 303, r.text
    loc = r.headers.get('location', '')
    assert 'prowlarr_sync=ok' in loc and 'imported=2' in loc, (
        f"redirect target should report imported count, got {loc!r}"
    )

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT name, type, url, api_key, parent_prowlarr_id, prowlarr_indexer_id,"
            " categories, min_seeders, seed_ratio"
            " FROM indexers WHERE parent_prowlarr_id=101 ORDER BY prowlarr_indexer_id"
        ).fetchall()
    assert len(rows) == 2, f"both selected subs should have rows, got {len(rows)}"

    nyaa, animebytes = rows[0], rows[1]
    assert nyaa['name'] == 'Nyaa'
    assert nyaa['type'] == 'torznab', "imported sub must be a torznab row"
    assert nyaa['url'] == 'http://prowlarr.test/1', (
        f"URL must be <base>/<sub-id> so the existing torznab fetcher's "
        f"f'{{url}}/api' resolves to Prowlarr's per-indexer façade; got {nyaa['url']!r}"
    )
    assert nyaa['api_key'] == 'fake-key', "API key must be copied from the parent"
    assert nyaa['prowlarr_indexer_id'] == 1
    # Categories should be the manga overlap with parent (7000+7010+7020 ∩ {7000, 7020} = {7000, 7020})
    assert _json.loads(nyaa['categories']) == [7000, 7020]
    # min_seeders/seed_ratio should propagate from the parent
    assert nyaa['min_seeders'] == 3
    assert nyaa['seed_ratio'] == 1.5


def test_sync_is_idempotent_on_resync(env):
    """Re-syncing the same selection must NOT create duplicate rows.
    The (parent_prowlarr_id, prowlarr_indexer_id) pair is the natural key."""
    client = _client()
    csrf = _csrf("sync-resync")

    with patch('httpx.AsyncClient', new=_mock_prowlarr(_FAKE_PROWLARR_RESPONSE)):
        # First sync — should import 2
        client.post(
            "/indexers/101/sync-prowlarr",
            data={'csrf_token': csrf['headers']['X-CSRFToken'],
                  'selected': ['1', '2']},
            **csrf, follow_redirects=False,
        )
        # Second sync — same selection. Must skip both.
        r2 = client.post(
            "/indexers/101/sync-prowlarr",
            data={'csrf_token': csrf['headers']['X-CSRFToken'],
                  'selected': ['1', '2']},
            **csrf, follow_redirects=False,
        )

    loc = r2.headers.get('location', '')
    assert 'imported=0' in loc and 'skipped=2' in loc, (
        f"re-sync should skip both already-imported subs, got {loc!r}"
    )

    with sqlite3.connect(env['db_path']) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM indexers WHERE parent_prowlarr_id=101"
        ).fetchone()[0]
    assert n == 2, f"re-sync must not duplicate; expected 2 rows total, got {n}"


def test_sync_with_no_selection_redirects_with_none_flag(env):
    """Submitting the form with zero checkboxes is a no-op, not an error."""
    client = _client()
    csrf = _csrf("sync-empty")

    r = client.post(
        "/indexers/101/sync-prowlarr",
        data={'csrf_token': csrf['headers']['X-CSRFToken']},
        **csrf, follow_redirects=False,
    )
    assert r.status_code == 303
    assert 'prowlarr_sync=none' in r.headers.get('location', '')

    with sqlite3.connect(env['db_path']) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM indexers WHERE parent_prowlarr_id IS NOT NULL"
        ).fetchone()[0]
    assert n == 0, "no rows should be created when nothing was selected"


def test_sync_rejects_non_prowlarr_parent(env):
    """Posting against a torznab-type indexer ID is rejected with a clear
    redirect flag — not a 500, not silent corruption."""
    client = _client()
    csrf = _csrf("sync-wrong-type")

    r = client.post(
        "/indexers/102/sync-prowlarr",
        data={'csrf_token': csrf['headers']['X-CSRFToken'],
              'selected': ['1']},
        **csrf, follow_redirects=False,
    )
    assert r.status_code == 303
    assert 'prowlarr_sync=invalid' in r.headers.get('location', '')

    with sqlite3.connect(env['db_path']) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM indexers WHERE parent_prowlarr_id=102"
        ).fetchone()[0]
    assert n == 0


def test_sync_skips_subs_that_vanished_between_preview_and_commit(env):
    """If Prowlarr's config changed between when the user opened the preview
    and clicked submit, the selected sub-id may no longer exist in the live
    list. Skip silently rather than fail the whole batch."""
    client = _client()
    csrf = _csrf("sync-vanished")

    with patch('httpx.AsyncClient', new=_mock_prowlarr(_FAKE_PROWLARR_RESPONSE)):
        # Submit sub-id 99 which was never in the response
        r = client.post(
            "/indexers/101/sync-prowlarr",
            data={'csrf_token': csrf['headers']['X-CSRFToken'],
                  'selected': ['1', '99']},
            **csrf, follow_redirects=False,
        )
    loc = r.headers.get('location', '')
    assert 'imported=1' in loc, (
        f"only the live sub (id=1) imported; vanished sub 99 silently skipped, got {loc!r}"
    )

    with sqlite3.connect(env['db_path']) as c:
        ids = [r[0] for r in c.execute(
            "SELECT prowlarr_indexer_id FROM indexers WHERE parent_prowlarr_id=101"
        ).fetchall()]
    assert ids == [1], f"only sub-id 1 should have a row, got {ids}"


def test_imported_subs_appear_on_indexers_page_with_attribution(env):
    """After import, /indexers should show the new rows with the
    'from Prowlarr (parent name)' badge — the visual confirmation that
    they're managed via Prowlarr but independently editable."""
    client = _client()
    csrf = _csrf("sync-attrib")

    with patch('httpx.AsyncClient', new=_mock_prowlarr(_FAKE_PROWLARR_RESPONSE)):
        client.post(
            "/indexers/101/sync-prowlarr",
            data={'csrf_token': csrf['headers']['X-CSRFToken'],
                  'selected': ['1']},
            **csrf, follow_redirects=False,
        )

    r = client.get("/indexers")
    assert r.status_code == 200
    body = r.text
    # Imported row appears
    assert 'Nyaa' in body
    # With attribution badge
    assert 'from Prowlarr' in body and 'TestProwlarr' in body, (
        "imported row should show 'from Prowlarr (TestProwlarr)' attribution badge"
    )
