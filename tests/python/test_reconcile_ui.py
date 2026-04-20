"""Contract tests for the series-scoped reconcile UI/API.

Builds on the tested backend (PR #40 → tests/python/test_map_drift_reconcile.py)
and the health panel wiring (PR #42 → tests/python/test_metadata_health_panel.py).
These tests only cover the new surface added in Prompt 3:

  - GET  /api/series/{id}/reconcile/preview   dry-run, JSON or HTMX partial
  - POST /api/series/{id}/reconcile/apply     explicit apply, safe moves only

And the template wiring:

  - Health panel surfaces a "Preview chapter re-map" button only when
    the series has actionable state.
  - Preview partial renders the summary + safe-moves table + apply
    button only when safe moves exist.
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
    key_dir = tempfile.mkdtemp(prefix="mangarr-reconui-keys-")

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
                 chapter_vol_map=None, title='Test'):
    with sqlite3.connect(db_path) as c:
        cvm = json.dumps(chapter_vol_map) if chapter_vol_map else None
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes,"
            " chapter_vol_map) VALUES(?,?,?,?,?)",
            (series_id, title, title, total_volumes, cvm)
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


# Fixture: a series with exactly one safe ok_move + one review-required
# row so preview / apply have something non-trivial to chew on.
def _seed_drifted_series(db_path):
    _seed_series(db_path, total_volumes=3,
                 chapter_vol_map={"5": 2, "6": 2})
    v1 = _seed_vol(db_path, volume_num=1.0, status='downloaded')
    _  = _seed_vol(db_path, volume_num=2.0, status='wanted')
    _  = _seed_vol(db_path, volume_num=3.0, status='wanted')
    # Two duplicate mainline vol 2s would make ch6 ambiguous; but we
    # only want one ambiguous row here, so build it with a separate
    # special-parent case for variety.
    special = _seed_vol(db_path, volume_num=1.0, status='grabbed',
                        is_special=1)
    safe_ch   = _seed_chap(db_path, chapter_num=5.0, volume_id=v1)
    review_ch = _seed_chap(db_path, chapter_num=6.0, volume_id=special)
    return {'safe_chapter_id': safe_ch, 'review_chapter_id': review_ch}


# ─────────────── 1. preview route ──────────────────────────────

def test_preview_route_returns_json_for_plain_callers(env):
    import main
    from fastapi.testclient import TestClient
    _seed_drifted_series(env)
    api_key = main.get_cfg("api_key")
    with TestClient(main.app) as client:
        r = client.get("/api/series/7/reconcile/preview",
                       headers={"X-Api-Key": api_key})
    assert r.status_code == 200
    body = r.json()
    assert body['series_id'] == 7
    assert body['ok_move'] == 1
    assert body['special_parent'] == 1
    # Safe rows must carry the full shape the UI relies on.
    safe = [r for r in body['rows'] if r['safe_to_apply']]
    assert len(safe) == 1
    row = safe[0]
    assert set(row.keys()) >= {
        'chapter_id', 'chapter_num',
        'current_volume_id', 'current_volume_num',
        'proposed_volume_id', 'proposed_volume_num',
        'safe_to_apply', 'requires_manual_review', 'reason',
    }


def test_preview_route_returns_partial_for_htmx(env):
    import main
    from fastapi.testclient import TestClient
    _seed_drifted_series(env)
    api_key = main.get_cfg("api_key")
    with TestClient(main.app) as client:
        r = client.get(
            "/api/series/7/reconcile/preview",
            headers={"X-Api-Key": api_key, "HX-Request": "true"},
        )
    assert r.status_code == 200
    html = r.text
    assert 'id="reconcile-preview-panel"' in html
    assert 'Apply 1 safe move' in html  # apply button visible
    # The review-flagged row must be listed in the details block, not
    # dropped silently.
    assert 'special_parent' in html


def test_preview_route_404_for_missing_series(env):
    import main
    from fastapi.testclient import TestClient
    api_key = main.get_cfg("api_key")
    with TestClient(main.app) as client:
        r = client.get("/api/series/99999/reconcile/preview",
                       headers={"X-Api-Key": api_key})
    assert r.status_code == 404


def test_preview_route_is_read_only(env):
    """GET /reconcile/preview must never mutate. The snapshots are
    taken INSIDE the TestClient context so the lifespan startup sweep
    (stuck-grabbed reset, queue retry) runs exactly once before we
    start diffing — otherwise a fresh-seeded row may get touched by
    startup tasks and poison the assertion."""
    import main
    from fastapi.testclient import TestClient
    _seed_drifted_series(env)
    api_key = main.get_cfg("api_key")

    with TestClient(main.app) as client:
        # Trigger lifespan once by doing a warm-up GET, then snapshot.
        client.get("/api/series/7/reconcile/preview",
                   headers={"X-Api-Key": api_key})

        with sqlite3.connect(env) as c:
            before = {t: list(c.execute(f"SELECT * FROM {t} ORDER BY id"))
                      for t in ('series', 'volumes', 'chapters', 'history')}

        for _ in range(3):
            client.get("/api/series/7/reconcile/preview",
                       headers={"X-Api-Key": api_key})

        with sqlite3.connect(env) as c:
            after = {t: list(c.execute(f"SELECT * FROM {t} ORDER BY id"))
                     for t in ('series', 'volumes', 'chapters', 'history')}
    assert before == after


# ─────────────── 2. apply route ────────────────────────────────

def _csrf_post(client, path, headers=None):
    """POST helper with a CSRF handshake — non-/api/... routes are
    CSRF-protected; /api/... are not, but keep the helper uniform."""
    client.get("/")
    hdrs = dict(headers or {})
    token = client.cookies.get("csrftoken") or ""
    if token:
        hdrs["X-CSRFToken"] = token
    return client.post(path, headers=hdrs)


def test_apply_route_moves_safe_rows_only(env):
    import main
    from fastapi.testclient import TestClient
    ids = _seed_drifted_series(env)
    api_key = main.get_cfg("api_key")

    with TestClient(main.app) as client:
        r = _csrf_post(client, "/api/series/7/reconcile/apply",
                       headers={"X-Api-Key": api_key})
    assert r.status_code == 200
    body = r.json()
    assert body['applied'] == 1
    assert body['skipped'] >= 1  # at least the review-required row

    # Safe row moved; review-required row did NOT.
    with sqlite3.connect(env) as c:
        vol_by_num = {r[1]: r[0] for r in c.execute(
            "SELECT id, volume_num FROM volumes WHERE series_id=7"
            " AND volume_num IS NOT NULL AND COALESCE(is_special,0)=0"
            " ORDER BY id"
        )}
        safe_link = c.execute(
            "SELECT volume_id FROM chapters WHERE id=?", (ids['safe_chapter_id'],)
        ).fetchone()[0]
        review_link = c.execute(
            "SELECT volume_id FROM chapters WHERE id=?", (ids['review_chapter_id'],)
        ).fetchone()[0]
    assert safe_link == vol_by_num[2.0], "safe row should point at vol 2"
    # Review row still points at the special vol (whatever id that is —
    # the important fact is it didn't flip to mainline).
    with sqlite3.connect(env) as c:
        is_special = c.execute(
            "SELECT is_special FROM volumes WHERE id=?", (review_link,)
        ).fetchone()[0]
    assert is_special == 1


def test_apply_route_returns_partial_for_htmx(env):
    """HTMX caller gets the fresh post-apply panel so the UI swaps
    in place showing 'No safe chapter moves'."""
    import main
    from fastapi.testclient import TestClient
    _seed_drifted_series(env)
    api_key = main.get_cfg("api_key")

    with TestClient(main.app) as client:
        r = _csrf_post(
            client, "/api/series/7/reconcile/apply",
            headers={"X-Api-Key": api_key, "HX-Request": "true"},
        )
    assert r.status_code == 200
    html = r.text
    assert 'id="reconcile-preview-panel"' in html
    assert 'Applied <strong>1</strong> safe chapter move' in html
    # After apply, the plan has zero ok_moves, so the apply button must
    # no longer be present.
    assert 'Apply 1 safe move' not in html
    assert 'No safe chapter moves' in html


def test_apply_route_is_idempotent(env):
    """Applying twice in a row — second call moves zero rows."""
    import main
    from fastapi.testclient import TestClient
    _seed_drifted_series(env)
    api_key = main.get_cfg("api_key")

    with TestClient(main.app) as client:
        first  = _csrf_post(client, "/api/series/7/reconcile/apply",
                            headers={"X-Api-Key": api_key}).json()
        second = _csrf_post(client, "/api/series/7/reconcile/apply",
                            headers={"X-Api-Key": api_key}).json()
    assert first['applied']  == 1
    assert second['applied'] == 0


def test_apply_route_writes_history_and_event_log(env):
    """Every safe move should leave a reconcile_chapter_vol history row
    (from the backend), plus the route logs an event_type='reconcile'
    summary entry via log_event."""
    import main
    from fastapi.testclient import TestClient
    _seed_drifted_series(env)
    api_key = main.get_cfg("api_key")

    with TestClient(main.app) as client:
        _csrf_post(client, "/api/series/7/reconcile/apply",
                   headers={"X-Api-Key": api_key})

    with sqlite3.connect(env) as c:
        c.row_factory = sqlite3.Row
        history_events = [dict(r) for r in c.execute(
            "SELECT * FROM history WHERE series_id=7"
            " AND event_type='reconcile_chapter_vol'"
        )]
        log_events = [dict(r) for r in c.execute(
            "SELECT * FROM events WHERE series_id=7"
            " AND event_type='reconcile'"
        )]
    assert history_events, "backend must write one history row per move"
    assert log_events, "route must emit a summary event via log_event"


def test_apply_route_404_for_missing_series(env):
    import main
    from fastapi.testclient import TestClient
    api_key = main.get_cfg("api_key")
    with TestClient(main.app) as client:
        r = _csrf_post(client, "/api/series/99999/reconcile/apply",
                       headers={"X-Api-Key": api_key})
    assert r.status_code == 404


def test_apply_route_rejects_get(env):
    """Apply is POST-only. A GET must not accidentally apply — Starlette
    auto-returns 405 Method Not Allowed for GET against a POST route."""
    import main
    from fastapi.testclient import TestClient
    _seed_drifted_series(env)
    api_key = main.get_cfg("api_key")
    with TestClient(main.app) as client:
        r = client.get("/api/series/7/reconcile/apply",
                       headers={"X-Api-Key": api_key})
    assert r.status_code == 405


def test_apply_does_not_touch_import_path_or_status(env):
    """Same contract as the backend test but asserted through the
    HTTP surface so a future route-layer regression can't sneak it in."""
    import main
    from fastapi.testclient import TestClient
    ids = _seed_drifted_series(env)
    api_key = main.get_cfg("api_key")

    # Poke the safe chapter with an import_path + status so we can
    # prove they survive the apply.
    with sqlite3.connect(env) as c:
        c.execute(
            "UPDATE chapters SET import_path='/lib/safe.cbz', status='downloaded'"
            " WHERE id=?", (ids['safe_chapter_id'],)
        )

    with TestClient(main.app) as client:
        r = _csrf_post(client, "/api/series/7/reconcile/apply",
                       headers={"X-Api-Key": api_key})
    assert r.status_code == 200

    with sqlite3.connect(env) as c:
        row = c.execute(
            "SELECT status, import_path FROM chapters WHERE id=?",
            (ids['safe_chapter_id'],)
        ).fetchone()
    assert row[0] == 'downloaded'
    assert row[1] == '/lib/safe.cbz'


# ─────────────── 3. UI wiring on the health panel ─────────────

def test_health_panel_shows_preview_button_for_drift(env):
    import main
    from fastapi.testclient import TestClient
    _seed_drifted_series(env)
    with TestClient(main.app) as client:
        r = client.get("/series/7")
    assert r.status_code == 200
    html = r.text
    # Preview button wired to the right endpoint with the right target.
    assert 'Preview chapter re-map' in html
    assert 'hx-get="/api/series/7/reconcile/preview"' in html
    assert 'hx-target="#reconcile-preview-panel"' in html


def test_health_panel_hides_preview_button_for_healthy(env):
    """Healthy series shouldn't invite the operator to open an empty
    preview panel — keeps the UI tidy."""
    import main
    from fastapi.testclient import TestClient
    _seed_series(env, total_volumes=1, chapter_vol_map={"1": 1})
    v1 = _seed_vol(env, volume_num=1.0, status='downloaded')
    _seed_chap(env, chapter_num=1.0, volume_id=v1)

    with TestClient(main.app) as client:
        r = client.get("/series/7")
    assert r.status_code == 200
    assert 'Preview chapter re-map' not in r.text


def test_series_detail_has_preview_panel_placeholder(env):
    """The empty preview container must exist so HTMX has a target on
    first click."""
    import main
    from fastapi.testclient import TestClient
    _seed_drifted_series(env)
    with TestClient(main.app) as client:
        r = client.get("/series/7")
    assert 'id="reconcile-preview-panel"' in r.text
