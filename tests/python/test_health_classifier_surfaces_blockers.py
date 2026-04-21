"""Bug #5 fix: _health_state must not return 'healthy' while the
readiness report still has non-empty blockers. Operators were seeing
state=healthy on series whose blockers listed missing_mainline_stubs
or unlinked_chapters, which contradicted the panel's own data.

Precedence:
    missing_metadata / no_mapping   (pre-existing; hard)
    needs_review (special/ambiguous drift rows)
    blocked_by_missing_volumes      (target_volume_missing rows)
    drift_detected                  (ok_move rows — actionable even
                                     if stub coverage isn't perfect)
    blocked_by_missing_volumes      (missing_mainline_stubs blocker
                                     with no drift to act on)
    needs_review                    (unlinked_chapters blocker)
    healthy
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


@pytest.fixture
def env():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-classifier-keys-")

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = db.name
    shared.DB_PATH = db.name
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()

    try:
        yield db.name
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = db.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _seed(db_path, *, series_id=7, total_volumes, cvm, vols, chapters=()):
    import json
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO series(id, title, search_pattern, total_volumes,"
            " total_chapters, chapter_vol_map) VALUES(?, 'S', 'S', ?, ?, ?)",
            (series_id, total_volumes, len(chapters) or None,
             json.dumps(cvm) if cvm else None)
        )
        vid = {}
        for v in vols:
            cur = c.execute(
                "INSERT INTO volumes(series_id, volume_num, status, monitored)"
                " VALUES(?, ?, 'wanted', 1)",
                (series_id, float(v))
            )
            vid[float(v)] = cur.lastrowid
        for ch_num, vol_num in chapters:
            vol_id = vid[float(vol_num)] if vol_num is not None else None
            c.execute(
                "INSERT INTO chapters(series_id, volume_id, chapter_num, status, monitored)"
                " VALUES(?, ?, ?, 'wanted', 1)",
                (series_id, vol_id, float(ch_num))
            )


def test_missing_stubs_without_drift_yields_blocked_state(env):
    from reconcile_map import build_metadata_health
    # total_volumes=5 but only vols 1,2 exist. cvm covers ch 1,2 → vol 1,2.
    # No drift (chapters already correct). Prior bug: state='healthy'
    # despite missing_mainline_stubs blocker.
    _seed(env, total_volumes=5, cvm={'1': 1, '2': 2}, vols=[1, 2],
          chapters=[(1, 1), (2, 2)])
    h = build_metadata_health(7)
    assert 'missing_mainline_stubs' in h['blockers']
    assert h['state'] == 'blocked_by_missing_volumes'


def test_unlinked_chapters_without_drift_yields_review_state(env):
    from reconcile_map import build_metadata_health
    # All stubs exist. cvm exists. But 1 chapter has no cvm entry
    # and sits with volume_id=NULL. Prior bug: state='healthy'.
    _seed(env, total_volumes=2, cvm={'1': 1, '2': 2}, vols=[1, 2],
          chapters=[(1, 1), (2, 2), (99, None)])
    h = build_metadata_health(7)
    assert 'unlinked_chapters' in h['blockers']
    assert h['state'] == 'needs_review'


def test_drift_still_wins_when_unrelated_stub_missing(env):
    from reconcile_map import build_metadata_health
    # total_volumes=5, only vols 1 and 2 exist (3-5 missing). cvm says
    # ch 5 → vol 2. Chapter 5 is currently linked to vol 1 (drift).
    # drift_detected should win; the missing stubs for vol 3-5 are
    # unrelated to the ok_move.
    _seed(env, total_volumes=5, cvm={'5': 2}, vols=[1, 2],
          chapters=[(5, 1)])
    h = build_metadata_health(7)
    assert h['reconcile']['ok_move'] == 1
    assert 'missing_mainline_stubs' in h['blockers']
    assert h['state'] == 'drift_detected'


def test_fully_healthy_still_returns_healthy(env):
    from reconcile_map import build_metadata_health
    _seed(env, total_volumes=2, cvm={'1': 1, '2': 2}, vols=[1, 2],
          chapters=[(1, 1), (2, 2)])
    h = build_metadata_health(7)
    assert h['blockers'] == []
    assert h['state'] == 'healthy'
