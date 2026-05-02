"""Tests for the per-profile CF score matrix (PR #125).

Sonarr's #1 unfixed CF UX pain ([Sonarr/Sonarr#7284], open since 2023):
editing 30 CFs across 4 profiles via the per-profile UI = 120 clicks
through nested modals. Mangarr's `quality_profile_custom_formats` join
table already supports many-to-many scoring; this PR adds the bulk-edit
matrix view.

Behavior pinned here:
  - GET /custom-formats/scores renders rows × profiles
  - POST submits all cells at once via score__<pid>__<fid> field naming
  - Score=0 deletes the row from the join table (keeps it sparse)
  - Score≠0 upserts (ON CONFLICT update)
  - No clobber: cells not submitted are left untouched
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture
def env(tmp_path):
    """Fresh DB; tests seed CFs + profiles + scores directly."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-cfmtx-keys-")

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


def _seed_grid(db_path):
    """Seed 3 CFs × 2 profiles + a couple of pre-existing scores."""
    with sqlite3.connect(db_path) as c:
        c.execute("DELETE FROM quality_profile_custom_formats")  # join rows from preset seed
        c.execute("DELETE FROM quality_profiles")  # init_db seeds default
        c.execute("DELETE FROM custom_formats")   # PR #127 seeds 11 CFs by default
        c.execute("INSERT INTO custom_formats(id, name) VALUES(1, 'cf-A'), (2, 'cf-B'), (3, 'cf-C')")
        c.execute(
            "INSERT INTO quality_profiles(id, name, qualities, cutoff)"
            " VALUES(10, 'P1', '[\"cbz\"]', 'cbz'),"
            "       (11, 'P2', '[\"cbz\"]', 'cbz')"
        )
        # Pre-existing scores — only some pairs have rows
        c.execute(
            "INSERT INTO quality_profile_custom_formats(profile_id, format_id, score)"
            " VALUES(10, 1, 100),"
            "       (10, 2, -50)"
        )


# ───────────────────── GET render ─────────────────────


def test_matrix_page_renders_full_grid(env):
    """The page must render every CF as a row and every profile as a column,
    even when no score row exists for the cell (empty cells render as 0)."""
    _seed_grid(env['db_path'])
    client = _client()
    r = client.get("/custom-formats/scores")
    assert r.status_code == 200, r.text
    body = r.text

    # All CF names appear (rows)
    assert 'cf-A' in body
    assert 'cf-B' in body
    assert 'cf-C' in body
    # All profile names appear (columns)
    assert 'P1' in body
    assert 'P2' in body

    # Pre-existing scores render in their cells
    assert 'value="100"' in body, "P1 × cf-A cell should show 100"
    assert 'value="-50"' in body, "P1 × cf-B cell should show -50"
    # Cells with no row default to 0
    assert 'name="score__11__3"' in body and 'value="0"' in body, (
        "P2 × cf-C cell with no existing row must render as value=0"
    )


def test_matrix_page_empty_state_when_no_formats(env):
    """If there are no CFs or no profiles, render an empty-state with
    pointers to the create pages instead of an empty table."""
    with sqlite3.connect(env['db_path']) as c:
        c.execute("DELETE FROM custom_formats")
        # leave profiles in place

    client = _client()
    r = client.get("/custom-formats/scores")
    assert r.status_code == 200
    body = r.text
    assert 'No custom formats' in body, "must render empty-state"
    assert '/custom-formats' in body, "must link to create page"


# ───────────────────── POST bulk-save ─────────────────────


def test_post_upserts_changed_scores(env):
    """Submitting a value for an existing cell updates the row in place."""
    _seed_grid(env['db_path'])
    client = _client()
    csrf = _csrf("upsert")

    r = client.post(
        "/custom-formats/scores",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'score__10__1': '500',  # cf-A on P1 — was 100, now 500
            'score__10__2': '-50',  # unchanged
            'score__10__3': '0',    # unchanged (no row)
            'score__11__1': '0',    # unchanged (no row)
            'score__11__2': '0',    # unchanged (no row)
            'score__11__3': '0',    # unchanged (no row)
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    with sqlite3.connect(env['db_path']) as c:
        score = c.execute(
            "SELECT score FROM quality_profile_custom_formats"
            " WHERE profile_id=10 AND format_id=1"
        ).fetchone()[0]
    assert score == 500, "P1 × cf-A must update to 500"


def test_post_inserts_new_scores_for_previously_empty_cells(env):
    """Submitting a non-zero value for a cell that had no row INSERTs."""
    _seed_grid(env['db_path'])
    client = _client()
    csrf = _csrf("insert")

    r = client.post(
        "/custom-formats/scores",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'score__11__3': '750',  # was empty, now 750
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303)

    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT score FROM quality_profile_custom_formats"
            " WHERE profile_id=11 AND format_id=3"
        ).fetchone()
    assert row is not None and row[0] == 750


def test_post_deletes_row_when_score_is_zero(env):
    """Submitting score=0 for an existing cell deletes the row — keeps the
    join table sparse for unused pairs."""
    _seed_grid(env['db_path'])
    client = _client()
    csrf = _csrf("delete")

    r = client.post(
        "/custom-formats/scores",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'score__10__1': '0',  # was 100, now 0 → delete
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303)

    with sqlite3.connect(env['db_path']) as c:
        row = c.execute(
            "SELECT 1 FROM quality_profile_custom_formats"
            " WHERE profile_id=10 AND format_id=1"
        ).fetchone()
    assert row is None, "score=0 must delete the join row"


def test_post_handles_negative_scores(env):
    """Negative scores (e.g., -10000 hard reject) must persist correctly."""
    _seed_grid(env['db_path'])
    client = _client()
    csrf = _csrf("negative")

    r = client.post(
        "/custom-formats/scores",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'score__11__1': '-10000',  # hard reject
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303)

    with sqlite3.connect(env['db_path']) as c:
        score = c.execute(
            "SELECT score FROM quality_profile_custom_formats"
            " WHERE profile_id=11 AND format_id=1"
        ).fetchone()[0]
    assert score == -10000


def test_post_ignores_unrelated_form_fields(env):
    """Form may carry CSRF tokens, etc. The handler must only act on
    `score__<pid>__<fid>` keys."""
    _seed_grid(env['db_path'])
    client = _client()
    csrf = _csrf("ignore")

    r = client.post(
        "/custom-formats/scores",
        data={
            'csrf_token': csrf['headers']['X-CSRFToken'],
            'score__10__3': '42',
            'unrelated': 'garbage',
            'score_typo__10__1': '999',  # malformed key, must be ignored
        },
        **csrf, follow_redirects=False,
    )
    assert r.status_code in (200, 303)

    with sqlite3.connect(env['db_path']) as c:
        # Only the well-formed key was applied
        rows = c.execute(
            "SELECT profile_id, format_id, score"
            " FROM quality_profile_custom_formats"
            " ORDER BY profile_id, format_id"
        ).fetchall()
    rows_set = set(rows)
    assert (10, 3, 42) in rows_set
    # The pre-seed rows should be untouched (handler only modifies submitted keys)
    assert (10, 1, 100) in rows_set
    assert (10, 2, -50) in rows_set


def test_navlink_appears_on_custom_formats_page(env):
    """Discoverability: the Score Matrix button must appear on
    /custom-formats so users find the new feature."""
    _seed_grid(env['db_path'])
    client = _client()
    r = client.get("/custom-formats")
    assert r.status_code == 200
    assert 'Score Matrix' in r.text
    assert '/custom-formats/scores' in r.text
