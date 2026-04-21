"""PR 6: the metadata-readiness report + health classifier surface
the 'extra mainline stubs' case where volume_num > total_volumes.
This came up in live data (JJK 31 vs 30, Death Note 14 vs 12, and
HxH pre-fix 40 vs 39) — the reconciler can't detect it because
ok_move stays at 0, so the prior classifier cheerfully returned
'healthy' despite a real invariant violation."""
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
    key_dir = tempfile.mkdtemp(prefix="mangarr-phantom-keys-")

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
    """Seed a series with its vols and chapters. cvm: dict[str,int]. vols:
    list of volume_num floats. chapters: list of (ch_num, vol_num)."""
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


def test_extra_stub_is_flagged_in_blockers(env):
    from reconcile_map import metadata_readiness_report
    # total_volumes=3 but vols 1..4 exist → vol 4 is an extra stub
    _seed(env, total_volumes=3,
          cvm={'1': 1, '2': 2, '3': 3},
          vols=[1, 2, 3, 4],
          chapters=[(1, 1), (2, 2), (3, 3)])
    r = metadata_readiness_report(7)
    assert 'extra_mainline_stubs' in r['blockers']
    assert r['extra_mainline_stubs'] == [4.0]


def test_extra_stub_surfaced_in_state(env):
    from reconcile_map import build_metadata_health
    _seed(env, total_volumes=3,
          cvm={'1': 1, '2': 2, '3': 3},
          vols=[1, 2, 3, 4],
          chapters=[(1, 1), (2, 2), (3, 3)])
    h = build_metadata_health(7)
    assert 'extra_mainline_stubs' in h['blockers']
    assert h['state'] == 'needs_review', (
        f"expected state=needs_review when extra stubs exist, got {h['state']}"
    )


def test_recommended_next_step_mentions_extras(env):
    from reconcile_map import metadata_readiness_report
    _seed(env, total_volumes=3,
          cvm={'1': 1, '2': 2, '3': 3},
          vols=[1, 2, 3, 4, 5],
          chapters=[(1, 1), (2, 2), (3, 3)])
    r = metadata_readiness_report(7)
    assert r['extra_mainline_stubs'] == [4.0, 5.0]
    assert '4' in r['recommended_next_step'] and '5' in r['recommended_next_step']


def test_drift_still_wins_when_extra_stubs_exist(env):
    """If the reconciler CAN act (ok_move > 0), the drift_detected
    state must still beat the extra-stub flag — operators should see
    the apply button even when a latent data issue exists."""
    from reconcile_map import build_metadata_health
    # ch 5 currently linked to vol 1, cvm says vol 2 → drift
    _seed(env, total_volumes=3,
          cvm={'5': 2},
          vols=[1, 2, 3, 4],  # vol 4 is an extra stub
          chapters=[(5, 1)])
    h = build_metadata_health(7)
    assert h['reconcile']['ok_move'] == 1
    assert 'extra_mainline_stubs' in h['blockers']
    assert h['state'] == 'drift_detected'


def test_no_extras_stays_healthy(env):
    from reconcile_map import build_metadata_health
    _seed(env, total_volumes=3,
          cvm={'1': 1, '2': 2, '3': 3},
          vols=[1, 2, 3],
          chapters=[(1, 1), (2, 2), (3, 3)])
    h = build_metadata_health(7)
    assert h['blockers'] == []
    assert h['extra_mainline_stubs'] == []
    assert h['state'] == 'healthy'
