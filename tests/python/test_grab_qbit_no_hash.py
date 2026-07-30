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
    db.close()
    os.unlink(db.name)
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
        yield {"db_path": db.name}
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _seed_series(
    db_path, series_id=1, title="Test Series", volume_num=5, anilist_id=None
) -> None:
    """Insert a series + a wanted volume row for the grab loop to target."""
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, monitored, status,"
            " anilist_id, total_volumes) VALUES(?, ?, ?, 1, 'RELEASING', ?, 30)",
            (series_id, title, title.lower(), anilist_id),
        )
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored)"
            " VALUES(?, ?, 'wanted', 1)",
            (series_id, volume_num),
        )


def _stub_grab_url(
    success: bool,
    client_healthy: bool,
    dl_id=None,
    client_name="qbittorrent",
    download_client_id: int | None = None,
):
    """Build an async stub with optional exact-client ownership."""

    async def _stub(
        url, protocol="", save_path=None, torrent_name=None, series_id=None
    ):
        if download_client_id is not None:
            from clients import GrabResult

            return GrabResult(
                success,
                client_name,
                dl_id,
                client_healthy,
                download_client_id,
            )
        return success, client_name, dl_id, client_healthy

    return _stub


def _build_item(url, title, vol_num=None):
    return {
        "url": url,
        "title": title,
        "indexer": "TestIndexer",
        "protocol": "torrent",
        "size_bytes": 100_000_000,
        "guid": f"guid-{vol_num}",
    }


# ───────────────────── grab_url tuple shape ─────────────────────


def test_grab_result_preserves_legacy_unpacking_and_exposes_client_id():
    import inspect
    from clients import GrabResult, grab_url

    sig = inspect.signature(grab_url)
    ann = str(sig.return_annotation)
    assert "GrabResult" in ann
    result = GrabResult(True, "qbittorrent", "hash", True, 42)
    assert tuple(result) == (True, "qbittorrent", "hash", True)
    assert result.download_client_id == 42


@pytest.mark.parametrize(
    ("protocol", "client_type", "adapter_name"),
    [
        ("torrent", "qbittorrent", "qbit_grab"),
        ("nzb", "sabnzbd", "sab_grab"),
    ],
)
def test_grab_url_returns_exact_selected_client_id(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    client_type: str,
    adapter_name: str,
) -> None:
    import clients
    import routers.download_clients as download_clients

    selected_id = 4_242
    monkeypatch.setattr(
        download_clients,
        "get_client_for_protocol",
        lambda *args, **kwargs: {
            "id": selected_id,
            "name": "Selected client",
            "type": client_type,
        },
    )
    monkeypatch.setattr(download_clients, "_cb_is_open", lambda _client_id: False)
    monkeypatch.setattr(download_clients, "_cb_record_success", lambda _client_id: None)
    monkeypatch.setattr(download_clients, "_cb_record_failure", lambda _client_id: None)

    async def _grab(*args, **kwargs):
        del args, kwargs
        return True, "download-id", True

    monkeypatch.setattr(clients, adapter_name, _grab)
    result = asyncio.run(
        clients.grab_url(
            "magnet:?xt=urn:btih:" + "a" * 40
            if protocol == "torrent"
            else "https://indexer.test/release.nzb",
            protocol,
        )
    )
    assert tuple(result) == (True, client_type, "download-id", True)
    assert result.download_client_id == selected_id


# ───────────────────── soft-failure dedup ─────────────────────


def test_qbit_no_hash_inserts_seen_for_url_dedup(env):
    import grab_core

    _seed_series(env["db_path"])

    item = _build_item(
        url="http://indexer.test/release-xyz.torrent",
        title="Test Series Vol 5",
        vol_num=5,
    )

    with patch.object(
        grab_core,
        "grab_url",
        _stub_grab_url(
            success=False,
            client_healthy=True,
            dl_id=None,
            download_client_id=101,
        ),
    ):
        result = asyncio.run(grab_core.grab_item(item, series_id=1))

    assert result is False, "soft-failure returns False (volume stays wanted)"

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        seen = c.execute(
            "SELECT torrent_url, download_id, download_client_id"
            " FROM seen WHERE torrent_url=?",
            (item["url"],),
        ).fetchone()
    assert seen is not None, (
        "seen row MUST be inserted on soft-failure to prevent the "
        "infinite RSS retry loop"
    )
    assert seen["download_id"] is None, "no hash → NULL download_id"
    assert seen["download_client_id"] == 101


def test_qbit_no_hash_does_not_mark_volume_grabbed(env):
    import grab_core

    _seed_series(env["db_path"], volume_num=7)

    item = _build_item(
        url="http://indexer.test/release-stays-wanted.torrent",
        title="Test Series Vol 7",
        vol_num=7,
    )

    with patch.object(
        grab_core,
        "grab_url",
        _stub_grab_url(success=False, client_healthy=True, dl_id=None),
    ):
        asyncio.run(grab_core.grab_item(item, series_id=1))

    with sqlite3.connect(env["db_path"]) as c:
        row = c.execute(
            "SELECT status, download_id FROM volumes WHERE series_id=1 AND volume_num=7"
        ).fetchone()
    assert row[0] == "wanted", (
        f"volume must stay 'wanted' on soft-failure; got {row[0]!r}"
    )
    assert row[1] is None


def test_qbit_hard_failure_does_NOT_dedup_url(env):
    import grab_core

    _seed_series(env["db_path"], volume_num=6)

    item = _build_item(
        url="http://indexer.test/release-abc.torrent",
        title="Test Series Vol 6",
        vol_num=6,
    )

    with patch.object(
        grab_core,
        "grab_url",
        _stub_grab_url(success=False, client_healthy=False, dl_id=None),
    ):
        asyncio.run(grab_core.grab_item(item, series_id=1))

    with sqlite3.connect(env["db_path"]) as c:
        seen = c.execute(
            "SELECT 1 FROM seen WHERE torrent_url=?", (item["url"],)
        ).fetchone()
    assert seen is None, (
        "hard failure must NOT seed seen; next poll should retry once "
        "the client recovers"
    )


def test_repeated_soft_failures_only_seed_seen_once(env):
    import grab_core

    _seed_series(env["db_path"], volume_num=9)

    item = _build_item(
        url="http://indexer.test/repeat-test.torrent",
        title="Test Series Vol 9",
        vol_num=9,
    )

    call_count = {"n": 0}

    async def _counting_stub(
        url, protocol="", save_path=None, torrent_name=None, series_id=None
    ):
        call_count["n"] += 1
        return False, "qbittorrent", None, True

    with patch.object(grab_core, "grab_url", _counting_stub):
        asyncio.run(grab_core.grab_item(item, series_id=1))
        asyncio.run(grab_core.grab_item(item, series_id=1))

    assert call_count["n"] == 1, (
        f"grab_url called {call_count['n']} times — the seen-dedup "
        "must short-circuit the second call"
    )


def test_qbit_full_success_still_marks_volume_grabbed(env):
    import grab_core

    _seed_series(env["db_path"], volume_num=8)
    with sqlite3.connect(env["db_path"]) as c:
        volume_id = c.execute(
            "SELECT id FROM volumes WHERE series_id=1 AND volume_num=8"
        ).fetchone()[0]
        c.execute(
            "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored)"
            " VALUES(1,?,80,'wanted',1)",
            (volume_id,),
        )

    item = _build_item(
        url="http://indexer.test/release-success.torrent",
        title="Test Series Vol 8",
        vol_num=8,
    )

    with patch.object(
        grab_core,
        "grab_url",
        _stub_grab_url(
            success=True,
            client_healthy=True,
            dl_id="abc123hashvalue",
            download_client_id=808,
        ),
    ):
        result = asyncio.run(grab_core.grab_item(item, series_id=1))

    assert result is True
    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        v = c.execute(
            "SELECT status, download_id, download_client_id"
            " FROM volumes WHERE series_id=1 AND volume_num=8"
        ).fetchone()
        s = c.execute(
            "SELECT download_id, download_client_id FROM seen WHERE torrent_url=?",
            (item["url"],),
        ).fetchone()
        chapter = c.execute(
            "SELECT status, download_id, download_client_id"
            " FROM chapters WHERE series_id=1 AND chapter_num=80"
        ).fetchone()
    assert v["status"] == "grabbed"
    assert v["download_id"] == "abc123hashvalue"
    assert v["download_client_id"] == 808
    assert s is not None and s["download_id"] == "abc123hashvalue"
    assert s["download_client_id"] == 808
    assert tuple(chapter) == ("grabbed", "abc123hashvalue", 808)
