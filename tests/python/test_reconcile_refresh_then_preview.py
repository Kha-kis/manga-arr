"""Contract tests for POST /api/series/{id}/reconcile/refresh-then-preview.

Motivation: the HxH pilot showed that a stale cached `chapter_vol_map`
can produce a misleading reconcile preview. Skipping the refresh can
lead to "safe moves" that are actually wrong. This one-click combined
action bakes "refresh first" into the default UI path.

These tests lock in the contract:
  - route refreshes the map before computing preview (proven via mock)
  - route is strictly series-scoped and does NOT apply reconcile
  - HTMX response includes both the preview partial and an OOB
    metadata-health swap
  - JSON response shape for plain callers
  - refresh failure returns a clear error response without touching
    reconcile state
  - button wiring on the health panel
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-rthenp-keys-")

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
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _seed(db_path, *, series_id=7, total_volumes=3,
          chapter_vol_map=None, mangadex_id='dummy-md-id',
          title='Test'):
    with sqlite3.connect(db_path) as c:
        cvm = json.dumps(chapter_vol_map) if chapter_vol_map else None
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes,"
            " chapter_vol_map, mangadex_id) VALUES(?,?,?,?,?,?)",
            (series_id, title, title, total_volumes, cvm, mangadex_id)
        )


def _seed_vol(db_path, *, series_id=7, volume_num, status='wanted',
              is_special=0):
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, is_special)"
            " VALUES(?,?,?,?)",
            (series_id, volume_num, status, is_special)
        )
        return cur.lastrowid


def _seed_chap(db_path, *, series_id=7, chapter_num, volume_id=None,
               status='downloaded'):
    with sqlite3.connect(db_path) as c:
        cur = c.execute(
            "INSERT INTO chapters(series_id, chapter_num, volume_id, status)"
            " VALUES(?,?,?,?)",
            (series_id, chapter_num, volume_id, status)
        )
        return cur.lastrowid


def _seed_drifted(db_path):
    """HxH-shaped fixture: chapter 5 currently linked to vol 1 while the
    cached map says vol 1 (no drift yet). The fake refresh will mutate
    the map so chapter 5 → vol 2, revealing the drift."""
    _seed(db_path, total_volumes=3, chapter_vol_map={"5": 1})
    v1 = _seed_vol(db_path, volume_num=1.0, status='downloaded')
    _  = _seed_vol(db_path, volume_num=2.0, status='wanted')
    _  = _seed_vol(db_path, volume_num=3.0, status='wanted')
    ch5 = _seed_chap(db_path, chapter_num=5.0, volume_id=v1)
    return {'v1_id': v1, 'ch5_id': ch5}


def _csrf_post(client, path, headers=None):
    """CSRF handshake helper — /api/ routes are CSRF-exempt but we keep
    the helper uniform for any path we throw at it."""
    client.get("/")
    hdrs = dict(headers or {})
    token = client.cookies.get("csrftoken") or ""
    if token:
        hdrs["X-CSRFToken"] = token
    return client.post(path, headers=hdrs)


# ─────────── 1. route refreshes the map before computing preview ──────

def test_refresh_then_preview_calls_refresh_mangadex_map(env):
    """Mock `refresh_mangadex_map` to mutate the cached map AND return True.
    After the route runs, the new preview must reflect the mutated map."""
    import main
    from fastapi.testclient import TestClient
    _seed_drifted(env)
    api_key = main.get_cfg("api_key")

    async def _fake_refresh(series_id):
        # Mutate series.chapter_vol_map to introduce drift (ch 5 → vol 2).
        with sqlite3.connect(env) as c:
            c.execute(
                "UPDATE series SET chapter_vol_map=? WHERE id=?",
                (json.dumps({"5": 2}), series_id)
            )
        return True

    with patch.object(main, 'refresh_mangadex_map', new=_fake_refresh):
        with TestClient(main.app) as client:
            r = _csrf_post(client,
                           "/api/series/7/reconcile/refresh-then-preview",
                           headers={"X-Api-Key": api_key})
    assert r.status_code == 200
    body = r.json()
    # Refresh was called (proven because cvm is now {"5": 2})
    with sqlite3.connect(env) as c:
        cvm = c.execute("SELECT chapter_vol_map FROM series WHERE id=7").fetchone()[0]
    assert json.loads(cvm) == {"5": 2}
    # Preview reflects the new map — ok_move present, was 0 before.
    assert body['refreshed'] is True
    assert body['plan']['ok_move'] == 1


# ─────────── 2. series-scoped: unknown id → 404, other series untouched

def test_refresh_then_preview_404_for_missing_series(env):
    import main
    from fastapi.testclient import TestClient
    api_key = main.get_cfg("api_key")
    with TestClient(main.app) as client:
        r = _csrf_post(client, "/api/series/99999/reconcile/refresh-then-preview",
                       headers={"X-Api-Key": api_key})
    assert r.status_code == 404


def test_refresh_then_preview_only_touches_target_series(env):
    """Seed two series. Refresh series 7 — series 8's map must be unchanged."""
    import main
    from fastapi.testclient import TestClient
    _seed_drifted(env)
    _seed(env, series_id=8, total_volumes=2, chapter_vol_map={"1": 1},
          title="Bystander", mangadex_id='other-md-id')
    api_key = main.get_cfg("api_key")

    async def _fake_refresh(series_id):
        with sqlite3.connect(env) as c:
            # Only refresh the target series, per the real helper's contract.
            c.execute(
                "UPDATE series SET chapter_vol_map=? WHERE id=?",
                (json.dumps({"5": 2}), series_id)
            )
        return True

    with patch.object(main, 'refresh_mangadex_map', new=_fake_refresh):
        with TestClient(main.app) as client:
            _csrf_post(client, "/api/series/7/reconcile/refresh-then-preview",
                       headers={"X-Api-Key": api_key})

    with sqlite3.connect(env) as c:
        cvm_other = c.execute(
            "SELECT chapter_vol_map FROM series WHERE id=8"
        ).fetchone()[0]
    assert json.loads(cvm_other) == {"1": 1}


# ─────────── 3. route does NOT apply reconcile ───────────────────────

def test_refresh_then_preview_does_not_apply(env):
    """The whole point is preview-only. Even with refresh succeeding and
    the plan showing ok_move>0, no chapter rows may be moved. The
    operator has to click Apply separately."""
    import main
    from fastapi.testclient import TestClient
    ids = _seed_drifted(env)
    api_key = main.get_cfg("api_key")

    async def _fake_refresh(series_id):
        with sqlite3.connect(env) as c:
            c.execute(
                "UPDATE series SET chapter_vol_map=? WHERE id=?",
                (json.dumps({"5": 2}), series_id)
            )
        return True

    with patch.object(main, 'refresh_mangadex_map', new=_fake_refresh):
        with TestClient(main.app) as client:
            r = _csrf_post(client, "/api/series/7/reconcile/refresh-then-preview",
                           headers={"X-Api-Key": api_key})
    assert r.status_code == 200
    assert r.json()['plan']['ok_move'] == 1

    # Chapter 5 must STILL point at v1 — no apply happened.
    with sqlite3.connect(env) as c:
        vol_id_after = c.execute(
            "SELECT volume_id FROM chapters WHERE id=?", (ids['ch5_id'],)
        ).fetchone()[0]
    assert vol_id_after == ids['v1_id'], (
        "refresh-then-preview must not apply — ch 5 should still point at vol 1"
    )


# ─────────── 4. HTMX response wiring ────────────────────────────────

def test_htmx_response_contains_preview_and_oob_health_panel(env):
    import main
    from fastapi.testclient import TestClient
    _seed_drifted(env)
    api_key = main.get_cfg("api_key")

    async def _fake_refresh(series_id):
        with sqlite3.connect(env) as c:
            c.execute(
                "UPDATE series SET chapter_vol_map=? WHERE id=?",
                (json.dumps({"5": 2}), series_id)
            )
        return True

    with patch.object(main, 'refresh_mangadex_map', new=_fake_refresh):
        with TestClient(main.app) as client:
            r = _csrf_post(
                client, "/api/series/7/reconcile/refresh-then-preview",
                headers={"X-Api-Key": api_key, "HX-Request": "true"},
            )
    assert r.status_code == 200
    html = r.text
    # Primary swap target: the preview panel.
    assert 'id="reconcile-preview-panel"' in html
    assert 'Apply 1 safe move' in html
    # OOB swap: metadata health panel with hx-swap-oob marker.
    assert 'id="metadata-health-panel"' in html
    assert 'hx-swap-oob="true"' in html


# ─────────── 5. JSON response shape ────────────────────────────────

def test_json_response_shape(env):
    import main
    from fastapi.testclient import TestClient
    _seed_drifted(env)
    api_key = main.get_cfg("api_key")

    async def _fake_refresh(series_id):
        with sqlite3.connect(env) as c:
            c.execute(
                "UPDATE series SET chapter_vol_map=? WHERE id=?",
                (json.dumps({"5": 2}), series_id)
            )
        return True

    with patch.object(main, 'refresh_mangadex_map', new=_fake_refresh):
        with TestClient(main.app) as client:
            r = _csrf_post(client, "/api/series/7/reconcile/refresh-then-preview",
                           headers={"X-Api-Key": api_key})
    body = r.json()
    for key in ('refreshed', 'state', 'chapter_vol_map_size', 'plan'):
        assert key in body, f"missing field {key!r} in JSON response"
    for k in ('ok_move', 'already_correct', 'no_map_entry',
              'target_volume_missing', 'target_ambiguous', 'special_parent'):
        assert k in body['plan'], f"missing plan counter {k!r}"


# ─────────── 6. refresh failure paths ────────────────────────────

def test_refresh_failure_returns_json_error_for_plain_caller(env):
    """refresh_mangadex_map raising → route returns 502 JSON with error."""
    import main
    from fastapi.testclient import TestClient
    _seed_drifted(env)
    api_key = main.get_cfg("api_key")

    async def _raising_refresh(series_id):
        raise ConnectionError("simulated mangadex outage")

    with patch.object(main, 'refresh_mangadex_map', new=_raising_refresh):
        with TestClient(main.app) as client:
            r = _csrf_post(client, "/api/series/7/reconcile/refresh-then-preview",
                           headers={"X-Api-Key": api_key})
    assert r.status_code == 502
    body = r.json()
    assert body['refreshed'] is False
    assert 'ConnectionError' in body['error']


def test_refresh_failure_returns_partial_for_htmx(env):
    """Same failure, HTMX caller → error partial renders inline without
    disturbing existing state."""
    import main
    from fastapi.testclient import TestClient
    _seed_drifted(env)
    api_key = main.get_cfg("api_key")

    async def _raising_refresh(series_id):
        raise RuntimeError("boom")

    with patch.object(main, 'refresh_mangadex_map', new=_raising_refresh):
        with TestClient(main.app) as client:
            r = _csrf_post(
                client, "/api/series/7/reconcile/refresh-then-preview",
                headers={"X-Api-Key": api_key, "HX-Request": "true"},
            )
    # Error partial returns 200 (error is shown inline, not as HTTP error).
    assert r.status_code == 200
    html = r.text
    assert 'id="reconcile-preview-panel"' in html
    assert 'Map refresh failed' in html
    assert 'RuntimeError' in html


def test_refresh_returning_false_returns_clear_error(env):
    """refresh_mangadex_map returning False (no mdx_id, validation failed,
    etc.) → caller sees a clear 'not refreshed' response, no apply, no
    preview state mutation."""
    import main
    from fastapi.testclient import TestClient
    ids = _seed_drifted(env)
    api_key = main.get_cfg("api_key")

    async def _false_refresh(series_id):
        return False

    with patch.object(main, 'refresh_mangadex_map', new=_false_refresh):
        with TestClient(main.app) as client:
            r = _csrf_post(client, "/api/series/7/reconcile/refresh-then-preview",
                           headers={"X-Api-Key": api_key})
    assert r.status_code == 200
    body = r.json()
    assert body['refreshed'] is False
    assert 'error' in body

    # Map and chapter link untouched.
    with sqlite3.connect(env) as c:
        cvm = c.execute(
            "SELECT chapter_vol_map FROM series WHERE id=7"
        ).fetchone()[0]
        vol_id = c.execute(
            "SELECT volume_id FROM chapters WHERE id=?", (ids['ch5_id'],)
        ).fetchone()[0]
    assert json.loads(cvm) == {"5": 1}
    assert vol_id == ids['v1_id']


# ─────────── 7. health-panel button wiring ────────────────────────

def test_health_panel_renders_refresh_button_for_actionable_series(env):
    """Series in drift_detected state → BOTH buttons visible on the
    health panel: 'Refresh map + preview' (safer default) and
    'Preview chapter re-map' (uses cached map)."""
    import main
    from fastapi.testclient import TestClient
    _seed(env, total_volumes=3, chapter_vol_map={"5": 2})
    v1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    _  = _seed_vol(env, volume_num=2.0, status='wanted')
    _  = _seed_vol(env, volume_num=3.0, status='wanted')
    _seed_chap(env, chapter_num=5.0, volume_id=v1)

    with TestClient(main.app) as client:
        r = client.get("/series/7")
    assert r.status_code == 200
    html = r.text
    # New button wired to the new endpoint.
    assert 'hx-post="/api/series/7/reconcile/refresh-then-preview"' in html
    assert 'Refresh map + preview' in html
    # Existing "cached-map-only" button still present.
    assert 'hx-get="/api/series/7/reconcile/preview"' in html
    assert 'Preview chapter re-map' in html


def test_health_panel_hides_refresh_button_for_healthy(env):
    """Healthy series shouldn't offer reconcile entry points at all."""
    import main
    from fastapi.testclient import TestClient
    _seed(env, total_volumes=1, chapter_vol_map={"1": 1})
    v1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    _seed_chap(env, chapter_num=1.0, volume_id=v1)

    with TestClient(main.app) as client:
        r = client.get("/series/7")
    assert r.status_code == 200
    assert 'Refresh map + preview' not in r.text
    assert 'Preview chapter re-map' not in r.text
