"""Tests for the new quality-profile upgrade gates (PR #124).

Sonarr v4 / Radarr v5 added two columns to the upgrade decision logic:

  cutoff_format_score      — once existing release's CF score is ≥ this,
                             no more CF-driven upgrades are pursued.
  min_upgrade_format_score — minimum delta (new_score − old_score) required
                             to trigger a CF upgrade. Sonarr's universal
                             answer to the perennial "I'm in a download
                             loop" support pain. Default 10 — ships
                             loop-prevention without user intervention.

These tests pin the new behavior in the upgrade engine + form persistence.
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
    """Fresh DB + a series with quality_profile_id seeded by each test."""
    import main, shared, security

    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-qp-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    library_root = tmp_path / "library"
    library_root.mkdir()

    with sqlite3.connect(db.name) as c:
        c.execute("DELETE FROM root_folders")
        c.execute(
            "INSERT INTO root_folders(id, path) VALUES(1, ?)", (str(library_root),)
        )
        c.execute(
            "INSERT INTO download_clients(name, type, host, enabled)"
            " VALUES('TestQbit', 'qbittorrent', 'http://stub', 1)"
        )

    try:
        yield {"db_path": db.name}
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
        "cookies": {"csrftoken": tok},
        "headers": {"X-CSRFToken": tok},
    }


# ───────────────────── schema defaults ─────────────────────


def test_new_quality_profile_defaults_loop_prevention(env):
    """A profile created with no overrides must default to:
       cutoff_format_score = 10000 (effectively unbounded)
       min_upgrade_format_score = 10 (loop prevention)
    These are the TRaSH-Guides-recommended Sonarr v4 defaults."""
    with sqlite3.connect(env["db_path"]) as c:
        c.execute(
            "INSERT INTO quality_profiles(name, qualities, cutoff)"
            " VALUES('Default', '[\"cbz\"]', 'cbz')"
        )
        row = c.execute(
            "SELECT cutoff_format_score, min_upgrade_format_score"
            " FROM quality_profiles WHERE name='Default'"
        ).fetchone()
    assert row[0] == 10000, "default cutoff_format_score must be 10000"
    assert row[1] == 10, (
        "default min_upgrade_format_score must be 10 — loop-prevention by default"
    )


# ───────────────────── form persistence ─────────────────────


def test_create_form_persists_new_columns(env):
    client = _client()
    csrf = _csrf("create")
    r = client.post(
        "/quality-profiles",
        data={
            "csrf_token": csrf["headers"]["X-CSRFToken"],
            "name": "TightProfile",
            "qualities": '["cbz","epub"]',
            "cutoff": "cbz",
            "upgrades_allowed": "1",
            "minimum_custom_format_score": "50",
            "cutoff_format_score": "500",
            "min_upgrade_format_score": "25",
        },
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env["db_path"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT cutoff_format_score, min_upgrade_format_score"
            " FROM quality_profiles WHERE name='TightProfile'"
        ).fetchone()
    assert row["cutoff_format_score"] == 500
    assert row["min_upgrade_format_score"] == 25


def test_edit_form_updates_new_columns(env):
    with sqlite3.connect(env["db_path"]) as c:
        c.execute("DELETE FROM quality_profiles")
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities, cutoff,"
            " cutoff_format_score, min_upgrade_format_score)"
            " VALUES(1, 'EditMe', '[\"cbz\"]', 'cbz', 10000, 10)"
        )

    client = _client()
    csrf = _csrf("edit")
    r = client.post(
        "/quality-profiles/1",
        data={
            "csrf_token": csrf["headers"]["X-CSRFToken"],
            "name": "EditMe",
            "qualities": '["cbz"]',
            "cutoff": "cbz",
            "upgrades_allowed": "1",
            "minimum_custom_format_score": "0",
            "cutoff_format_score": "750",
            "min_upgrade_format_score": "50",
        },
        **csrf,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env["db_path"]) as c:
        row = c.execute(
            "SELECT cutoff_format_score, min_upgrade_format_score"
            " FROM quality_profiles WHERE id=1"
        ).fetchone()
    assert row[0] == 750
    assert row[1] == 50


# ───────────────────── upgrade engine ─────────────────────


def _seed_series_with_profile(db_path, *, profile_kwargs=None):
    """Helper: create a series, profile, and a downloaded volume vol=1.0
    with quality='cbr' (lower than cutoff='cbz') so quality-driven upgrades
    are blocked and the engine falls through to the CF-score path."""
    profile_kwargs = profile_kwargs or {}
    cfs = profile_kwargs.get("cutoff_format_score", 10000)
    mufs = profile_kwargs.get("min_upgrade_format_score", 10)
    with sqlite3.connect(db_path) as c:
        c.execute("DELETE FROM quality_profiles")
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities, cutoff,"
            " cutoff_format_score, min_upgrade_format_score)"
            " VALUES(1, 'P', '[\"cbz\",\"cbr\"]', 'cbz', ?, ?)",
            (cfs, mufs),
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode, root_folder_id, quality_profile_id,"
            " update_strategy)"
            " VALUES(1, 'TestSeries', 'TestSeries', 'standard', 1, 1, 'all', 1, 1, 'always')"
        )
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, monitored,"
            " quality, torrent_name, release_group)"
            " VALUES(11, 1, 1.0, 'downloaded', 1, 'cbz',"
            " 'TestSeries v01 [GoodGroup]', 'GoodGroup')"
        )


def test_cutoff_format_score_blocks_further_cf_upgrades(env):
    """Once existing release's CF score >= cutoff_format_score, the engine
    must reject CF-driven upgrades for the same volume."""
    import main

    _seed_series_with_profile(
        env["db_path"],
        profile_kwargs={
            "cutoff_format_score": 100,  # low ceiling
            "min_upgrade_format_score": 10,
        },
    )

    item = {
        "url": "https://stub/upgrade.torrent",
        "title": "TestSeries v01 [BetterGroup]",
        "protocol": "torrent",
        "guid": "g1",
        "indexer": "X",
        "_score": 100,
    }

    async def _ok(*a, **kw):
        return (True, "TestQbit", "dl-1", True)

    # Stub score_release: existing scores 150 (above cutoff 100), new scores 200.
    # cutoff_format_score blocks regardless of delta.
    def _fake_score(title, *a, **kw):
        if "BetterGroup" in title:
            return 200
        return 150

    async def _run():
        import grab_core

        with (
            patch.object(grab_core, "grab_url", _ok),
            patch.object(grab_core, "score_release", _fake_score),
        ):
            return await main.grab_item(item, series_id=1)

    result = asyncio.run(_run())
    assert result is False, (
        "upgrade must be rejected — existing score 150 >= cutoff 100, "
        "no further CF upgrades pursued"
    )


def test_min_upgrade_score_blocks_trivial_delta_upgrades(env):
    """Sonarr's #1 download-loop fix: reject upgrades with delta below
    min_upgrade_format_score. Same-release re-grab_cores for +1 score gain
    must not happen."""
    import main

    _seed_series_with_profile(
        env["db_path"],
        profile_kwargs={
            "cutoff_format_score": 10000,
            "min_upgrade_format_score": 50,  # require ≥50 point delta
        },
    )

    item = {
        "url": "https://stub/tinyupgrade.torrent",
        "title": "TestSeries v01 [SameGroup]",
        "protocol": "torrent",
        "guid": "g2",
        "indexer": "X",
        "_score": 100,
    }

    async def _ok(*a, **kw):
        return (True, "TestQbit", "dl-2", True)

    # Stub: old=200, new=210 → delta=10, below min 50 → reject
    def _fake_score(title, *a, **kw):
        if "SameGroup" in title:
            return 210
        return 200

    async def _run():
        import grab_core

        with (
            patch.object(grab_core, "grab_url", _ok),
            patch.object(grab_core, "score_release", _fake_score),
        ):
            return await main.grab_item(item, series_id=1)

    result = asyncio.run(_run())
    assert result is False, (
        "trivial-delta upgrade (+10 vs min 50) must be rejected — "
        "this is Sonarr's universal download-loop fix"
    )


def test_upgrade_with_sufficient_delta_proceeds(env):
    """Sanity: when delta meets min_upgrade_format_score AND old score is
    below cutoff_format_score, the upgrade is allowed."""
    import main

    _seed_series_with_profile(
        env["db_path"],
        profile_kwargs={
            "cutoff_format_score": 10000,
            "min_upgrade_format_score": 10,
        },
    )

    item = {
        "url": "https://stub/realupgrade.torrent",
        "title": "TestSeries v01 [BestGroup]",
        "protocol": "torrent",
        "guid": "g3",
        "indexer": "X",
        "_score": 100,
    }

    async def _ok(*a, **kw):
        return (True, "TestQbit", "dl-3", True)

    # Stub: old=100, new=200 → delta=100, well above min 10
    def _fake_score(title, *a, **kw):
        if "BestGroup" in title:
            return 200
        return 100

    async def _run():
        import grab_core

        with (
            patch.object(grab_core, "grab_url", _ok),
            patch.object(grab_core, "score_release", _fake_score),
        ):
            return await main.grab_item(item, series_id=1)

    result = asyncio.run(_run())
    assert result is True, "+100 delta well above min 10 should allow the upgrade"


def test_cutoff_takes_precedence_over_min_upgrade_when_both_set(env):
    """When old_score >= cutoff_format_score, upgrade is rejected even if
    delta would be huge. Cutoff is the harder ceiling."""
    import main

    _seed_series_with_profile(
        env["db_path"],
        profile_kwargs={
            "cutoff_format_score": 100,  # tight ceiling
            "min_upgrade_format_score": 10,
        },
    )

    item = {
        "url": "https://stub/blocked.torrent",
        "title": "TestSeries v01 [Group]",
        "protocol": "torrent",
        "guid": "g4",
        "indexer": "X",
        "_score": 100,
    }

    async def _ok(*a, **kw):
        return (True, "TestQbit", "dl-4", True)

    # Stub: old=500 (well above cutoff 100), new=1000.
    # Delta would be 500 (huge), but cutoff blocks regardless.
    def _fake_score(title, *a, **kw):
        if "Group" in title:
            return 1000
        return 500

    async def _run():
        import grab_core

        with (
            patch.object(grab_core, "grab_url", _ok),
            patch.object(grab_core, "score_release", _fake_score),
        ):
            return await main.grab_item(item, series_id=1)

    result = asyncio.run(_run())
    assert result is False, (
        "cutoff_format_score is the harder ceiling — must block even when "
        "delta would otherwise allow the upgrade"
    )


def test_existing_indexer_is_unchanged_for_quality_driven_upgrade(env):
    """If new release has STRICTLY HIGHER quality than old, the engine
    still allows the upgrade — the new CF gates only apply to the score-
    based path (when qualities are the same)."""
    import main

    # Seed with old volume at quality='cbr' (lower than cutoff 'cbz')
    with sqlite3.connect(env["db_path"]) as c:
        c.execute("DELETE FROM quality_profiles")
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities, cutoff,"
            " cutoff_format_score, min_upgrade_format_score)"
            " VALUES(1, 'P', '[\"cbz\",\"cbr\"]', 'cbz', 100, 10)"
        )
        c.execute(
            "INSERT INTO series(id, title, search_pattern, edition_type, enabled,"
            " monitored, monitor_mode, root_folder_id, quality_profile_id,"
            " update_strategy)"
            " VALUES(1, 'TS', 'TS', 'standard', 1, 1, 'all', 1, 1, 'always')"
        )
        c.execute("INSERT INTO root_folders(path) VALUES('/tmp')")
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, monitored,"
            " quality, torrent_name, release_group)"
            " VALUES(11, 1, 1.0, 'downloaded', 1, 'cbr',"
            " 'TS v01.cbr', 'OldGroup')"
        )

    item = {
        "url": "https://stub/quality-upgrade.cbz",
        "title": "TS v01.cbz",
        "protocol": "torrent",
        "guid": "qu",
        "indexer": "X",
        "_score": 100,
    }

    async def _ok(*a, **kw):
        return (True, "TestQbit", "dl-qu", True)

    async def _run():
        import grab_core

        with patch.object(grab_core, "grab_url", _ok):
            return await main.grab_item(item, series_id=1)

    result = asyncio.run(_run())
    assert result is True, (
        "quality-driven upgrade (cbr → cbz) must NOT be gated by the new "
        "CF-score columns — those only apply to same-quality, score-based path"
    )
