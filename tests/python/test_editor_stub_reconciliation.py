"""Bug #3 fix: the series editor must reconcile volume-stub coverage
whenever a cvm update could leave vols unrepresented, not only when
total_volumes increases.

Live-session context: Vinland Saga got a wiki-derived cvm via the
editor (total_volumes=29, unchanged). The editor skipped
create_volume_stubs because total_volumes didn't increase, so vols
15..28 remained uninstantiated and the subsequent reconcile preview
reported 109 target_volume_missing. We had to run a one-off
vs_fix_stubs helper to close the gap. This test pins the fix so the
workaround is never needed again.
"""
import json
import os
import sqlite3
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-edit-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    main.ensure_api_key()

    # Seed a series with total_volumes set but no stubs beyond vol 1.
    # This mirrors the "cvm drift pushed more vols than stubs exist" case.
    with sqlite3.connect(db.name) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes)"
            " VALUES(?, ?, ?, ?)",
            (42, "Stub Gap Series", "Stub Gap", 10)
        )
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, monitored)"
            " VALUES(?, ?, 'wanted', 1)",
            (42, 1.0)
        )

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _client():
    import main
    return TestClient(main.app)


def _post_edit(client, *, series_id: int, chapter_map_text: str = "",
               total_volumes: int = 0) -> int:
    # Double-submit CSRF — send both cookie and form token set to the
    # same value, same pattern the live tests used.
    tok = "test-csrf-" + "a" * 32
    data = {
        "csrf_token":             tok,
        "title":                  "Stub Gap Series",
        "search_pattern":         "Stub Gap",
        "preferred_groups_input": "",
        "blocked_groups_input":   "",
        "omnibus_preference":     "prefer_individual",
        "quality_profile_id":     "0",
        "language_profile_id":    "0",
        "quality_cutoff":         "",
        "update_strategy":        "always",
        "required_scanlator":     "",
        "source_type":            "any",
        "edition_type":           "standard",
        "total_volumes":          str(total_volumes),
        "ddl_language":           "",
        "chapter_map_text":       chapter_map_text,
    }
    resp = client.post(
        f"/series/{series_id}/edit", data=data,
        cookies={"csrftoken": tok},
        headers={"X-CSRFToken": tok},
        follow_redirects=False,
    )
    return resp.status_code


def _mainline_vol_count(db_path: str, series_id: int) -> int:
    with sqlite3.connect(db_path) as c:
        return c.execute(
            "SELECT COUNT(*) FROM volumes WHERE series_id=?"
            " AND volume_num IS NOT NULL AND COALESCE(is_special,0)=0",
            (series_id,)
        ).fetchone()[0]


def test_editor_creates_missing_stubs_on_cvm_update_without_tvol_change(env):
    # Baseline: only 1 stub exists even though total_volumes=10.
    assert _mainline_vol_count(env, 42) == 1

    client = _client()
    # Submit a cvm that spans all 10 volumes. total_volumes stays 10.
    map_text = "\n".join(f"{i}" for i in range(1, 11))  # 10 lines → vols 1..10
    code = _post_edit(client, series_id=42, chapter_map_text=map_text,
                      total_volumes=10)
    assert code == 303, f"expected 303 redirect, got {code}"

    # Bug-#3 fix: stubs should now cover all 10 vols.
    assert _mainline_vol_count(env, 42) == 10
    with sqlite3.connect(env) as db:
        source, updated_at = db.execute(
            "SELECT chapter_map_source,chapter_map_updated_at FROM series WHERE id=42"
        ).fetchone()
    assert source == "manual"
    assert updated_at


def test_editor_still_creates_stubs_when_total_volumes_increases(env):
    # Regression guard — existing behaviour must keep working.
    assert _mainline_vol_count(env, 42) == 1
    client = _client()
    code = _post_edit(client, series_id=42, total_volumes=10)
    assert code == 303
    assert _mainline_vol_count(env, 42) == 10


def test_editor_is_idempotent_when_called_without_gap(env):
    # Fill all stubs first, then edit without changing anything.
    client = _client()
    _post_edit(client, series_id=42, total_volumes=10)
    assert _mainline_vol_count(env, 42) == 10
    # Second edit — no new inserts.
    _post_edit(client, series_id=42, total_volumes=10)
    assert _mainline_vol_count(env, 42) == 10
