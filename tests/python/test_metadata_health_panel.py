"""Contract tests for the series-page metadata health panel.

Covers the three layers:

  1. ``build_metadata_health(series_id)`` classifies series into the
     six health states and never mutates the DB.
  2. ``GET /api/series/{id}/metadata-health`` returns JSON for plain
     callers and the rendered partial for HTMX callers.
  3. The series detail page embeds the panel inline on first paint
     without 500ing when metadata is sparse.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-mhpanel-keys-")
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


def _seed_series(db_path, *, series_id=7, total_volumes=None,
                 total_chapters=None, chapter_vol_map=None, title="Test"):
    with sqlite3.connect(db_path) as c:
        cvm = json.dumps(chapter_vol_map) if chapter_vol_map else None
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes,"
            " total_chapters, chapter_vol_map)"
            " VALUES(?,?,?,?,?,?)",
            (series_id, title, title, total_volumes, total_chapters, cvm)
        )


def _seed_vol(db_path, *, series_id=7, volume_num=None, status='wanted',
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


# ─────────────── 1. state classification ──────────────────────────

def test_healthy_state(env):
    """Every chapter correctly linked and the map matches reality."""
    from reconcile_map import build_metadata_health
    _seed_series(env, total_volumes=2, chapter_vol_map={"1": 1, "2": 1, "3": 2})
    v1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    v2 = _seed_vol(env, volume_num=2.0, status='downloaded')
    _seed_chap(env, chapter_num=1.0, volume_id=v1)
    _seed_chap(env, chapter_num=2.0, volume_id=v1)
    _seed_chap(env, chapter_num=3.0, volume_id=v2)

    mh = build_metadata_health(7)
    assert mh['state'] == 'healthy'
    assert mh['reconcile']['ok_move'] == 0
    assert mh['reconcile']['target_volume_missing'] == 0


def test_missing_metadata_state(env):
    """total_volumes absent → state is missing_metadata regardless of
    whatever the chapters table looks like."""
    from reconcile_map import build_metadata_health
    _seed_series(env, total_volumes=None, chapter_vol_map={"1": 1})
    _seed_vol(env, volume_num=1.0, status='downloaded')
    mh = build_metadata_health(7)
    assert mh['state'] == 'missing_metadata'


def test_no_mapping_state(env):
    """total_volumes fine but chapter_vol_map absent."""
    from reconcile_map import build_metadata_health
    _seed_series(env, total_volumes=3, chapter_vol_map=None)
    for v in (1, 2, 3):
        _seed_vol(env, volume_num=float(v))
    mh = build_metadata_health(7)
    assert mh['state'] == 'no_mapping'


def test_blocked_by_missing_volumes_state(env):
    """Map says chapter 5 → vol 2, but vol 2 has no mainline row.
    Reconciler reports target_volume_missing → state is blocked."""
    from reconcile_map import build_metadata_health
    _seed_series(env, total_volumes=5, chapter_vol_map={"5": 2})
    v1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    # Intentionally no vol 2 row.
    _seed_chap(env, chapter_num=5.0, volume_id=v1)
    mh = build_metadata_health(7)
    # total_volumes IS set, so missing_metadata doesn't fire even though
    # the series has a stub gap (the panel surfaces this separately).
    assert mh['state'] == 'blocked_by_missing_volumes'
    assert mh['reconcile']['target_volume_missing'] >= 1


def test_drift_detected_state(env):
    """Chapter linked to wrong vol, map + target row both present → ok_move."""
    from reconcile_map import build_metadata_health
    _seed_series(env, total_volumes=3, chapter_vol_map={"5": 2})
    v1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    _  = _seed_vol(env, volume_num=2.0, status='wanted')
    _  = _seed_vol(env, volume_num=3.0, status='wanted')
    _seed_chap(env, chapter_num=5.0, volume_id=v1)
    mh = build_metadata_health(7)
    assert mh['state'] == 'drift_detected'
    assert mh['reconcile']['ok_move'] >= 1


def test_needs_review_state_for_special_parent(env):
    """Chapter belongs to a special volume but map wants it in mainline →
    requires_manual_review → state is needs_review."""
    from reconcile_map import build_metadata_health
    _seed_series(env, total_volumes=3, chapter_vol_map={"5": 2})
    special_v = _seed_vol(env, volume_num=1.0, status='downloaded',
                          is_special=1)
    _          = _seed_vol(env, volume_num=2.0, status='wanted')
    _          = _seed_vol(env, volume_num=3.0, status='wanted')
    _seed_chap(env, chapter_num=5.0, volume_id=special_v)
    mh = build_metadata_health(7)
    assert mh['state'] == 'needs_review'
    assert mh['reconcile']['special_parent'] >= 1


def test_needs_review_state_for_target_ambiguous(env):
    """Two mainline rows share a target volume_num → ambiguous → review."""
    from reconcile_map import build_metadata_health
    _seed_series(env, total_volumes=3, chapter_vol_map={"5": 2})
    v1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    _  = _seed_vol(env, volume_num=2.0, status='downloaded')
    _  = _seed_vol(env, volume_num=2.0, status='wanted')   # duplicate mainline
    _seed_chap(env, chapter_num=5.0, volume_id=v1)
    mh = build_metadata_health(7)
    assert mh['state'] == 'needs_review'
    assert mh['reconcile']['target_ambiguous'] >= 1


# ─────────────── 2. contract: read-only ──────────────────────────

def test_build_metadata_health_never_mutates(env):
    """The panel payload must be strictly read-only. Diff the whole DB
    before/after repeated calls."""
    from reconcile_map import build_metadata_health
    _seed_series(env, total_volumes=3, chapter_vol_map={"5": 2})
    v1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    _  = _seed_vol(env, volume_num=2.0, status='wanted')
    _seed_chap(env, chapter_num=5.0, volume_id=v1)

    with sqlite3.connect(env) as c:
        before = {t: list(c.execute(
            f"SELECT * FROM {t} ORDER BY id")) for t in ('series', 'volumes', 'chapters')}

    for _ in range(3):
        build_metadata_health(7)

    with sqlite3.connect(env) as c:
        after = {t: list(c.execute(
            f"SELECT * FROM {t} ORDER BY id")) for t in ('series', 'volumes', 'chapters')}
    assert before == after


def test_unknown_series_returns_not_found_payload(env):
    """build_metadata_health on a missing series_id returns a 'not found'
    style payload (title is None) — this shape is what the API route
    relies on to return 404."""
    from reconcile_map import build_metadata_health
    mh = build_metadata_health(99999)
    assert mh['title'] is None
    assert mh['state'] == 'unknown'


# ─────────────── 3. HTTP route ──────────────────────────────────

def test_route_returns_json_for_plain_callers(env):
    """GET /api/series/{id}/metadata-health with no HX header returns
    the full JSON payload — useful for ad-hoc scripts and tests."""
    from fastapi.testclient import TestClient
    import main
    _seed_series(env, total_volumes=3, chapter_vol_map={"5": 2})
    v1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    _  = _seed_vol(env, volume_num=2.0, status='wanted')
    _seed_chap(env, chapter_num=5.0, volume_id=v1)
    api_key = main.get_cfg("api_key")

    with TestClient(main.app) as client:
        r = client.get(f"/api/series/7/metadata-health",
                       headers={"X-Api-Key": api_key})
    assert r.status_code == 200
    body = r.json()
    assert body['series_id'] == 7
    assert 'state' in body
    assert 'reconcile' in body
    assert body['reconcile']['ok_move'] >= 1


def test_route_returns_partial_for_htmx_callers(env):
    """Same endpoint, HX-Request=true → rendered HTML fragment suitable
    for an hx-swap=outerHTML refresh in-place."""
    from fastapi.testclient import TestClient
    import main
    _seed_series(env, total_volumes=1, chapter_vol_map={"1": 1})
    _seed_vol(env, volume_num=1.0, status='downloaded')
    api_key = main.get_cfg("api_key")

    with TestClient(main.app) as client:
        r = client.get(
            "/api/series/7/metadata-health",
            headers={"X-Api-Key": api_key, "HX-Request": "true"},
        )
    assert r.status_code == 200
    html = r.text
    assert 'id="metadata-health-panel"' in html
    assert 'Metadata health' in html


def test_route_404s_for_missing_series(env):
    from fastapi.testclient import TestClient
    import main
    api_key = main.get_cfg("api_key")
    with TestClient(main.app) as client:
        r = client.get("/api/series/99999/metadata-health",
                       headers={"X-Api-Key": api_key})
    assert r.status_code == 404


# ─────────────── 4. inline panel on series detail ───────────────

def test_series_detail_renders_panel_inline(env):
    """First paint of /series/{id} must include the panel without a
    round-trip. A bug in build_metadata_health must not 500 the page —
    the route wraps the helper in a try/except."""
    from fastapi.testclient import TestClient
    import main
    _seed_series(env, total_volumes=3, chapter_vol_map={"5": 2},
                 title="DriftSeries")
    v1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    _  = _seed_vol(env, volume_num=2.0, status='wanted')
    _seed_chap(env, chapter_num=5.0, volume_id=v1)

    with TestClient(main.app) as client:
        r = client.get("/series/7")
    assert r.status_code == 200
    html = r.text
    assert 'id="metadata-health-panel"' in html
    # In the drift_detected state the panel must show the drift pill,
    # not fall through to the muted "unavailable" rendering.
    assert 'Metadata health' in html
    assert 'Drift detected' in html


def test_series_detail_renders_for_sparse_metadata(env):
    """A series with null total_volumes + null chapter_vol_map must still
    render — the panel shows 'missing_metadata' rather than blowing up."""
    from fastapi.testclient import TestClient
    import main
    _seed_series(env, total_volumes=None, chapter_vol_map=None,
                 title="SparseSeries")

    with TestClient(main.app) as client:
        r = client.get("/series/7")
    assert r.status_code == 200
    assert 'id="metadata-health-panel"' in r.text
    assert 'Missing metadata' in r.text


def test_panel_refresh_button_targets_same_element(env):
    """The refresh button's hx-target must be the panel itself — so the
    click replaces the panel in place rather than appending or
    re-targeting something unexpected. Pin the outerHTML contract."""
    from fastapi.testclient import TestClient
    import main
    _seed_series(env, total_volumes=1, chapter_vol_map={"1": 1})
    _seed_vol(env, volume_num=1.0, status='downloaded')

    with TestClient(main.app) as client:
        r = client.get("/series/7")
    html = r.text
    # Refresh button wiring
    assert 'hx-get="/api/series/7/metadata-health"' in html
    assert 'hx-target="#metadata-health-panel"' in html
    assert 'hx-swap="outerHTML"' in html
