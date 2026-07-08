"""Regression test for the RSS-feed GUID parsing dedup gap.

Production bug observed: same torrent (same hash) was grabbed 9 times
for the Berserk series because Prowlarr session-rotated each download
URL but the torznab/Prowlarr-search RSS parsers in
`app/routers/indexers.py` never extracted the `<guid>` element. With
no guid in the item dict, `grab.py:148`'s `_release_guid = item.get('guid')`
fell back to None, and the URL-only dedup (different URL each poll)
silently bypassed everything.

The fix: both parsers (`_parse_torznab_rss` for XML, the Prowlarr-JSON
results loop) now populate the `guid` field. grab.py's existing
`SELECT 1 FROM seen WHERE release_guid=?` check then catches the
duplicate.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


def test_torznab_rss_parser_extracts_guid():
    """The XML <guid> element must end up in the item dict so grab.py's
    GUID-dedup can fire."""
    from routers.indexers import _parse_torznab_rss

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss xmlns:torznab="http://torznab.com/api/2015/feed">
      <channel>
        <item>
          <title>Berserk Vol 41 [Scans]</title>
          <link>http://prowlarr/dl?session=abc123</link>
          <guid isPermaLink="false">berserk-v41-unique-id-1234</guid>
          <torznab:attr name="size" value="50000000"/>
          <torznab:attr name="seeders" value="42"/>
        </item>
      </channel>
    </rss>"""

    items = _parse_torznab_rss(xml, indexer="Test", default_protocol="torrent")
    assert len(items) == 1
    assert items[0]["guid"] == "berserk-v41-unique-id-1234", (
        f"guid not extracted: {items[0]!r}"
    )


def test_torznab_rss_parser_falls_back_to_torznab_guid_attr():
    """Some indexers surface guid via torznab:attr name='guid' instead
    of the standard <guid> element."""
    from routers.indexers import _parse_torznab_rss

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss xmlns:torznab="http://torznab.com/api/2015/feed">
      <channel>
        <item>
          <title>Berserk Vol 41 [Scans]</title>
          <link>http://x/dl</link>
          <torznab:attr name="size" value="50000000"/>
          <torznab:attr name="guid" value="attr-style-guid-987"/>
        </item>
      </channel>
    </rss>"""
    items = _parse_torznab_rss(xml, indexer="Test")
    assert len(items) == 1
    assert items[0]["guid"] == "attr-style-guid-987"


def test_torznab_rss_parser_empty_guid_when_missing():
    """If no guid is provided, the field must be present-but-empty so
    callers can `item.get('guid')` without KeyError."""
    from routers.indexers import _parse_torznab_rss

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss>
      <channel>
        <item>
          <title>No GUID Release</title>
          <link>http://x/dl</link>
        </item>
      </channel>
    </rss>"""
    items = _parse_torznab_rss(xml, indexer="Test")
    assert len(items) == 1
    assert "guid" in items[0]
    assert items[0]["guid"] == ""


# ───────────────────── End-to-end: grab dedup ─────────────────────


@pytest.fixture
def env(tmp_path):
    import main, shared, security, sqlite3

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-rss-guid-")
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()
    try:
        yield {"db_path": db.name}
    finally:
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def test_same_guid_different_url_blocks_re_grab(env):
    """The actual production bug. Two items with different URLs but the
    same guid must dedup at the GUID layer — grab_item must short-
    circuit on the second call."""
    import asyncio
    import sqlite3
    from unittest.mock import patch
    import grab_core

    # Seed a series + wanted volume
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, monitored, status,"
            " total_volumes) VALUES(1, 'Test', 'test', 1, 'RELEASING', 5)"
        )
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored)"
            " VALUES(1, 3, 'wanted', 1)"
        )

    # Stub grab_url to claim success
    async def _ok_stub(url, **kw):
        return True, "qbittorrent", "hash-" + url[-6:], True

    item_v1 = {
        "url": "http://prowlarr/dl?session=session-A",
        "title": "Test Vol 3",
        "indexer": "Test",
        "protocol": "torrent",
        "size_bytes": 50_000_000,
        "guid": "release-12345",  # SAME guid
    }
    item_v2 = {
        "url": "http://prowlarr/dl?session=session-B",  # DIFFERENT url
        "title": "Test Vol 3",
        "indexer": "Test",
        "protocol": "torrent",
        "size_bytes": 50_000_000,
        "guid": "release-12345",  # SAME guid
    }

    call_count = {"n": 0}

    async def _counting_stub(
        url, protocol="", save_path=None, torrent_name=None, series_id=None
    ):
        call_count["n"] += 1
        return True, "qbittorrent", f"hash-{call_count['n']}", True

    with patch.object(grab_core, "grab_url", _counting_stub):
        # First grab: should succeed and insert seen with guid
        asyncio.run(grab_core.grab_item(item_v1, series_id=1))
        # Second grab: same guid, different URL → must short-circuit
        # at the GUID-dedup layer in grab_item:152
        asyncio.run(grab_core.grab_item(item_v2, series_id=1))

    assert call_count["n"] == 1, (
        f"grab_url called {call_count['n']} times — GUID dedup must "
        "short-circuit the second call (different URL but same guid)"
    )

    # Verify only one seen row exists for this content
    with sqlite3.connect(env["db_path"]) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM seen WHERE release_guid='release-12345'"
        ).fetchone()[0]
    assert n == 1, f"expected exactly 1 seen row, got {n}"
