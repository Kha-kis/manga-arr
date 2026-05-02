"""Tests for per-indexer min/max release size (PR #123).

Per the upstream Sonarr/Radarr community research, this is the most
frequently requested per-indexer feature that Sonarr explicitly does
NOT have. Two columns added to indexers:

  min_size_mb — releases smaller than this MB are rejected (0 = no floor)
  max_size_mb — releases larger than this MB are rejected  (0 = no ceiling)

Layered on top of the existing global `indexer_max_size` setting — the
tighter of the two applies effectively.

Real workflow: a private tracker that only carries complete-volume packs
(50–200 MB+) shouldn't deliver tiny chapter rips that pollute scoring;
a free public tracker that only carries single-chapter releases shouldn't
deliver huge bundles that overflow the user's quota.
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
    """Fresh DB; tests seed indexers + stub fetcher returning items
    with various sizes."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-size-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

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


def _csrf(tag="t"):
    tok = f"csrf-{tag}-" + "x" * 30
    return {
        'cookies': {'csrftoken': tok},
        'headers': {'X-CSRFToken': tok},
    }


# Sizes: 5MB tiny, 50MB medium, 500MB large
_TINY  = 5  * 1024 * 1024
_MED   = 50 * 1024 * 1024
_LARGE = 500 * 1024 * 1024


# ───────────────────── filter behavior ─────────────────────


def test_max_size_rejects_oversized_releases(env):
    """An indexer with max_size_mb=100 must skip 500MB items."""
    from routers.indexers import fetch_all_rss
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled,"
            " min_size_mb, max_size_mb)"
            " VALUES(1, 'CapAt100', 'torznab', 'http://t', 'k', 1, 0, 100)"
        )

    async def _stub_fetch(idx):
        return [
            {'url': 'http://x/tiny',  'title': 'A', 'protocol': 'torrent', 'size_bytes': _TINY},
            {'url': 'http://x/med',   'title': 'B', 'protocol': 'torrent', 'size_bytes': _MED},
            {'url': 'http://x/large', 'title': 'C', 'protocol': 'torrent', 'size_bytes': _LARGE},
        ]

    with patch('routers.indexers._fetch_rss_for_indexer', _stub_fetch):
        with get_db() as db:
            items = asyncio.run(fetch_all_rss(db))

    urls = [i['url'] for i in items]
    assert 'http://x/tiny'  in urls, "5MB item must pass (no min)"
    assert 'http://x/med'   in urls, "50MB item must pass (under 100MB ceiling)"
    assert 'http://x/large' not in urls, "500MB item must be rejected"


def test_min_size_rejects_undersized_releases(env):
    """An indexer with min_size_mb=20 must skip the 5MB item."""
    from routers.indexers import fetch_all_rss
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled,"
            " min_size_mb, max_size_mb)"
            " VALUES(2, 'FloorAt20', 'torznab', 'http://t', 'k', 1, 20, 0)"
        )

    async def _stub_fetch(idx):
        return [
            {'url': 'http://y/tiny',  'title': 'A', 'protocol': 'torrent', 'size_bytes': _TINY},
            {'url': 'http://y/med',   'title': 'B', 'protocol': 'torrent', 'size_bytes': _MED},
        ]

    with patch('routers.indexers._fetch_rss_for_indexer', _stub_fetch):
        with get_db() as db:
            items = asyncio.run(fetch_all_rss(db))

    urls = [i['url'] for i in items]
    assert 'http://y/tiny' not in urls, "5MB item < 20MB floor must be rejected"
    assert 'http://y/med'  in urls, "50MB item passes the floor"


def test_min_and_max_combined_window(env):
    """min=10MB, max=100MB → only items in [10MB, 100MB] survive."""
    from routers.indexers import fetch_all_rss
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled,"
            " min_size_mb, max_size_mb)"
            " VALUES(3, 'WindowedTracker', 'torznab', 'http://t', 'k', 1, 10, 100)"
        )

    async def _stub_fetch(idx):
        return [
            {'url': 'http://z/tiny',  'title': 'A', 'protocol': 'torrent', 'size_bytes': _TINY},
            {'url': 'http://z/med',   'title': 'B', 'protocol': 'torrent', 'size_bytes': _MED},
            {'url': 'http://z/large', 'title': 'C', 'protocol': 'torrent', 'size_bytes': _LARGE},
        ]

    with patch('routers.indexers._fetch_rss_for_indexer', _stub_fetch):
        with get_db() as db:
            items = asyncio.run(fetch_all_rss(db))

    urls = [i['url'] for i in items]
    assert 'http://z/tiny'  not in urls
    assert 'http://z/med'   in urls
    assert 'http://z/large' not in urls


def test_zero_size_filters_apply_no_limit(env):
    """The default (0/0) means no size constraint — all items pass."""
    from routers.indexers import fetch_all_rss
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled,"
            " min_size_mb, max_size_mb)"
            " VALUES(4, 'NoLimits', 'torznab', 'http://t', 'k', 1, 0, 0)"
        )

    async def _stub_fetch(idx):
        return [
            {'url': 'http://w/tiny',  'title': 'A', 'protocol': 'torrent', 'size_bytes': _TINY},
            {'url': 'http://w/large', 'title': 'C', 'protocol': 'torrent', 'size_bytes': _LARGE},
        ]

    with patch('routers.indexers._fetch_rss_for_indexer', _stub_fetch):
        with get_db() as db:
            items = asyncio.run(fetch_all_rss(db))

    urls = {i['url'] for i in items}
    assert 'http://w/tiny'  in urls
    assert 'http://w/large' in urls, "0 max means no ceiling"


def test_item_with_unknown_size_passes_when_min_is_set(env):
    """Items where size_bytes is missing/0 (some indexers don't report) must
    pass even if a min_size_mb is configured — we can't reject what we can't
    measure. Otherwise we'd miss legitimately good releases from indexers
    with poor metadata."""
    from routers.indexers import fetch_all_rss
    from shared import get_db

    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled,"
            " min_size_mb, max_size_mb)"
            " VALUES(5, 'PoorMetadata', 'torznab', 'http://t', 'k', 1, 50, 0)"
        )

    async def _stub_fetch(idx):
        return [
            {'url': 'http://m/unknown', 'title': 'A', 'protocol': 'torrent', 'size_bytes': 0},
            {'url': 'http://m/missing', 'title': 'B', 'protocol': 'torrent'},  # no size_bytes key
        ]

    with patch('routers.indexers._fetch_rss_for_indexer', _stub_fetch):
        with get_db() as db:
            items = asyncio.run(fetch_all_rss(db))

    urls = [i['url'] for i in items]
    assert 'http://m/unknown' in urls, "size=0 must pass min check (unknown size)"
    assert 'http://m/missing' in urls, "missing size_bytes key must pass min check"


# ───────────────────── form persistence ─────────────────────


def test_create_form_persists_size_limits(env):
    client = _client()
    csrf = _csrf("create-size")
    r = client.post(
        "/indexers",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'Sized',
            'type': 'torznab',
            'url': 'http://test',
            'api_key': 'k',
            'priority': '25',
            'enabled': '1',
            'categories': '7000',
            'use_rss': '1',
            'use_auto_search': '1',
            'use_interactive_search': '1',
            'min_size_mb': '10',
            'max_size_mb': '500',
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT min_size_mb, max_size_mb FROM indexers WHERE name='Sized'"
        ).fetchone()
    assert row['min_size_mb'] == 10
    assert row['max_size_mb'] == 500


def test_edit_form_clamps_negatives_to_zero(env):
    """Form submits negative values get clamped to 0 (= no limit)."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute(
            "INSERT INTO indexers(id, name, type, url, api_key, enabled,"
            " min_size_mb, max_size_mb)"
            " VALUES(50, 'EditMe', 'torznab', 'http://t', 'k', 1, 100, 200)"
        )

    client = _client()
    csrf = _csrf("edit-clamp")
    r = client.post(
        "/indexers/50",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'name': 'EditMe',
            'type': 'torznab',
            'url': 'http://t',
            'priority': '25',
            'enabled': '1',
            'categories': '7000',
            'keep_api_key': '1',
            'use_rss': '1',
            'use_auto_search': '1',
            'use_interactive_search': '1',
            'min_size_mb': '-5',
            'max_size_mb': '-1',
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT min_size_mb, max_size_mb FROM indexers WHERE id=50"
        ).fetchone()
    assert row['min_size_mb'] == 0, "negative min must clamp to 0"
    assert row['max_size_mb'] == 0, "negative max must clamp to 0"
