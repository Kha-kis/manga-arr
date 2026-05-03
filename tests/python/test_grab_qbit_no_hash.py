"""Regression test for the qBit "added but hash not found" infinite loop.

Production bug observed on a real install: when qBit accepted a torrent
but Mangarr couldn't find its hash via the post-add lookup (long titles,
sanitization differences), `qbit_grab` returned (False, None, True)
and `grab_url` propagated that as ok=False. The caller in
`grab.grab_item` short-circuited on `if not ok: return False` without
inserting a `seen` row.

Result: every RSS poll re-found the same URL, no dedup fired, qBit
re-accepted the torrent (or rejected with "Fails."), and the
"[qBit] grab added but hash not found for: ..." log spammed every
minute forever.

The fix: when grab_url returns (ok=False, client_healthy=True, dl_id=None),
treat that as a soft failure — insert `seen` so URL dedup blocks future
polls, but don't mark volumes (the orphan-cleanup loop in
import_pipeline would reset them anyway).

This file pins the dedup behaviour so the loop never re-fires.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB; tests stub network calls."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-grab-nohash-")

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


def _seed_series(db_path, series_id=1, title='Test Series', volume_num=5,
                 anilist_id=None) -> None:
    """Insert a series + a wanted volume row for the grab loop to target."""
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, monitored, status,"
            " anilist_id, total_volumes) VALUES(?, ?, ?, 1, 'RELEASING', ?, 30)",
            (series_id, title, title.lower(), anilist_id)
        )
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored)"
            " VALUES(?, ?, 'wanted', 1)",
            (series_id, volume_num)
        )


def _stub_grab_url(success: bool, client_healthy: bool, dl_id=None,
                   client_name='qbittorrent'):
    """Build an async stub that mimics grab_url's 4-tuple return."""
    async def _stub(url, protocol='', save_path=None, torrent_name=None,
                    series_id=None):
        return success, client_name, dl_id, client_healthy
    return _stub


def _build_item(url, title, vol_num=None):
    return {
        'url':         url,
        'title':       title,
        'indexer':     'TestIndexer',
        'protocol':    'torrent',
        'size_bytes':  100_000_000,
        'guid':        f'guid-{vol_num}',
    }


# ───────────────────── grab_url tuple shape ─────────────────────


def test_grab_url_returns_4_tuple_with_healthy_flag():
    """The signature change: grab_url now returns
    (ok, client_name, dl_id, client_healthy) — caller needs the
    healthy flag to distinguish soft vs hard failure."""
    import inspect
    from clients import grab_url
    sig = inspect.signature(grab_url)
    ann = str(sig.return_annotation)
    assert 'tuple' in ann.lower(), f"return annotation: {ann!r}"
    # 3 commas = 4 elements in the tuple
    assert ann.count(',') == 3, f"expected 4-tuple shape, got {ann!r}"


# ───────────────────── soft-failure dedup ─────────────────────


def test_qbit_no_hash_inserts_seen_for_url_dedup(env):
    """The bug: when qBit accepts the torrent but Mangarr can't find
    its hash, the caller used to return False without inserting `seen`.
    Now `seen` is inserted with download_id=NULL, blocking the
    URL-based dedup on the next RSS poll."""
    import grab as grab_mod
    _seed_series(env['db_path'])

    item = _build_item(
        url='http://indexer.test/release-xyz.torrent',
        title='Test Series Vol 5',
        vol_num=5,
    )

    with patch.object(grab_mod, 'grab_url',
                      _stub_grab_url(success=False, client_healthy=True, dl_id=None)):
        result = asyncio.run(grab_mod.grab_item(item, series_id=1))

    assert result is False, "soft-failure returns False (volume stays wanted)"

    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        seen = c.execute(
            "SELECT torrent_url, download_id FROM seen WHERE torrent_url=?",
            (item['url'],)
        ).fetchone()
    assert seen is not None, (
        "seen row MUST be inserted on soft-failure to prevent the "
        "infinite RSS retry loop"
    )
    assert seen['download_id'] is None, "no hash → NULL download_id"


def test_qbit_no_hash_does_not_mark_volume_grabbed(env):
    """Volume must stay 'wanted' on soft-failure. Marking it 'grabbed'
    with download_id=NULL would trigger the orphan-cleanup loop in
    import_pipeline.py:562 to reset it back to wanted on the next
    sweep — that fight produces "wanted ↔ grabbed" flapping."""
    import grab as grab_mod
    _seed_series(env['db_path'], volume_num=7)

    item = _build_item(
        url='http://indexer.test/release-stays-wanted.torrent',
        title='Test Series Vol 7',
        vol_num=7,
    )

    with patch.object(grab_mod, 'grab_url',
                      _stub_grab_url(success=False, client_healthy=True, dl_id=None)):
        asyncio.run(grab_mod.grab_item(item, series_id=1))

    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT status, download_id FROM volumes"
            " WHERE series_id=1 AND volume_num=7"
        ).fetchone()
    assert row[0] == 'wanted', (
        f"volume must stay 'wanted' on soft-failure; got {row[0]!r}"
    )
    assert row[1] is None


def test_qbit_hard_failure_does_NOT_dedup_url(env):
    """If qBit is genuinely unreachable (auth fail, connection refused,
    etc.), `client_healthy=False` and we should NOT insert `seen` —
    the next RSS poll should retry once the client recovers."""
    import grab as grab_mod
    _seed_series(env['db_path'], volume_num=6)

    item = _build_item(
        url='http://indexer.test/release-abc.torrent',
        title='Test Series Vol 6',
        vol_num=6,
    )

    with patch.object(grab_mod, 'grab_url',
                      _stub_grab_url(success=False, client_healthy=False, dl_id=None)):
        asyncio.run(grab_mod.grab_item(item, series_id=1))

    with sqlite3.connect(env['db_path']) as c:
        seen = c.execute(
            "SELECT 1 FROM seen WHERE torrent_url=?", (item['url'],)
        ).fetchone()
    assert seen is None, (
        "hard failure must NOT seed seen; next poll should retry once "
        "the client recovers"
    )


def test_repeated_soft_failures_only_seed_seen_once(env):
    """Idempotency: if grab_item is called twice with the same URL on
    the soft-failure path, the second call must short-circuit on the
    existing seen entry (already URL-deduped) and not retry the
    download client. This is the actual fix that stops the loop."""
    import grab as grab_mod
    _seed_series(env['db_path'], volume_num=9)

    item = _build_item(
        url='http://indexer.test/repeat-test.torrent',
        title='Test Series Vol 9',
        vol_num=9,
    )

    call_count = {'n': 0}

    async def _counting_stub(url, protocol='', save_path=None,
                             torrent_name=None, series_id=None):
        call_count['n'] += 1
        return False, 'qbittorrent', None, True

    with patch.object(grab_mod, 'grab_url', _counting_stub):
        # First poll: hits the stub, seen gets inserted
        asyncio.run(grab_mod.grab_item(item, series_id=1))
        # Second poll: should NOT call the client again (URL-deduped)
        asyncio.run(grab_mod.grab_item(item, series_id=1))

    assert call_count['n'] == 1, (
        f"grab_url called {call_count['n']} times — the seen-dedup "
        "must short-circuit the second call"
    )


def test_qbit_full_success_still_marks_volume_grabbed(env):
    """Regression: the normal success path must continue to work —
    seen inserted, volume marked grabbed with download_id."""
    import grab as grab_mod
    _seed_series(env['db_path'], volume_num=8)

    item = _build_item(
        url='http://indexer.test/release-success.torrent',
        title='Test Series Vol 8',
        vol_num=8,
    )

    with patch.object(grab_mod, 'grab_url',
                      _stub_grab_url(success=True, client_healthy=True,
                                     dl_id='abc123hashvalue')):
        result = asyncio.run(grab_mod.grab_item(item, series_id=1))

    assert result is True
    with sqlite3.connect(env['db_path']) as c:
        c.row_factory = sqlite3.Row
        v = c.execute(
            "SELECT status, download_id FROM volumes"
            " WHERE series_id=1 AND volume_num=8"
        ).fetchone()
        s = c.execute(
            "SELECT download_id FROM seen WHERE torrent_url=?",
            (item['url'],)
        ).fetchone()
    assert v['status'] == 'grabbed'
    assert v['download_id'] == 'abc123hashvalue'
    assert s is not None and s['download_id'] == 'abc123hashvalue'
