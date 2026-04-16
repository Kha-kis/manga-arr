"""Tests for app/verify_e2e.py refactored as a diagnostic library.

Each test builds a fixture DB containing a specific kind of state-machine
residue and asserts that diagnose() classifies it correctly. Empty DBs
must not crash (this was a real bug — the old script raised TypeError on
empty SUM() results).
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401  — sets up sys.path so we can import main


@pytest.fixture
def fresh_db():
    """Empty DB with the production schema. Each test seeds its own residue."""
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-diag-keys-")

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


def _diagnose(db_path):
    # Late import so the conftest path-setup runs first.
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))
    import verify_e2e
    return verify_e2e.diagnose(db_path)


def _by_code(findings):
    return {f.code: f for f in findings}


# ───────────────────────── empty DB safety ───────────────────────────────────

def test_diagnose_empty_db_does_not_crash(fresh_db):
    """The original script crashed with TypeError on empty SUM() results.
    The library must handle empty DBs cleanly and report all-zero counts."""
    findings = _diagnose(fresh_db)
    by_code = _by_code(findings)
    assert by_code["grabbed_volumes_total"].count == 0
    assert by_code["downloaded_volumes_total"].count == 0
    assert by_code["grabbed_chapters_total"].count == 0
    assert by_code["downloaded_chapters_total"].count == 0
    assert by_code["orphan_chapters"].count == 0
    assert by_code["orphan_volumes"].count == 0


def test_diagnose_missing_db_returns_critical(tmp_path):
    """Pointing at a path that doesn't exist must fail loudly, not crash."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))
    import verify_e2e
    findings = verify_e2e.diagnose(str(tmp_path / "nonexistent.db"))
    assert any(f.severity == "critical" and f.code == "db_missing"
               for f in findings)


# ───────────────────────── stuck-grabbed detection ───────────────────────────

def test_diagnose_detects_stuck_grabbed_volumes(fresh_db):
    """Volume in 'grabbed' status with grabbed_at >2 days ago surfaces as warning."""
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, grabbed_at, torrent_name,"
            " indexer, protocol, source_url) "
            " VALUES(?,?,?, datetime('now', '-5 days'), ?, ?, ?, ?)",
            (1, 1, "grabbed", "name.torrent", "MockIndexer", "torrent",
             "magnet:?xt=urn:btih:" + "a"*40)
        )

    findings = _diagnose(fresh_db)
    by_code = _by_code(findings)
    assert by_code["stuck_grabbed_total"].count == 1
    assert by_code["stuck_grabbed_warning"].severity == "warning"


def test_diagnose_recent_grabbed_is_not_stuck(fresh_db):
    """Volumes grabbed within the threshold should not flag."""
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, grabbed_at, torrent_name,"
            " indexer, protocol, source_url) "
            " VALUES(?,?,?, datetime('now', '-1 hour'), ?, ?, ?, ?)",
            (1, 1, "grabbed", "name.torrent", "MockIndexer", "torrent",
             "magnet:?xt=urn:btih:" + "b"*40)
        )

    findings = _diagnose(fresh_db)
    by_code = _by_code(findings)
    assert by_code["stuck_grabbed_total"].count == 0
    assert "stuck_grabbed_warning" not in by_code


# ───────────────────────── ghost-downloaded chapter detection ────────────────

def test_diagnose_detects_ghost_downloaded_chapters(fresh_db):
    """A chapter in status='downloaded' with no quality + no import_path is
    a ghost — recorded as downloaded but the file/info is missing."""
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute("INSERT INTO volumes(id, series_id, volume_num, status) VALUES(1, 1, 1, 'downloaded')")
        # Two chapters: one healthy, one ghost
        c.execute("INSERT INTO chapters(series_id, volume_id, chapter_num, status, quality, import_path)"
                  " VALUES(?,?,?,?,?,?)", (1, 1, 1.0, "downloaded", "WEB-DL", "/data/ch1.cbz"))
        c.execute("INSERT INTO chapters(series_id, volume_id, chapter_num, status, quality, import_path)"
                  " VALUES(?,?,?,?,?,?)", (1, 1, 2.0, "downloaded", None, None))

    findings = _diagnose(fresh_db)
    by_code = _by_code(findings)
    assert by_code["downloaded_chapters_total"].count == 2
    assert "ghost_downloaded_chapters" in by_code
    assert by_code["ghost_downloaded_chapters"].count == 1
    assert by_code["ghost_downloaded_chapters"].severity == "warning"


# ───────────────────────── import queue residue ──────────────────────────────

def test_diagnose_classifies_import_queue_states(fresh_db):
    """failed / importing / partial rows each surface as warnings."""
    with sqlite3.connect(fresh_db) as c:
        for status, n in (("failed", 2), ("importing", 1), ("partial", 1),
                          ("done", 5)):  # done is fine, no warning expected
            for _ in range(n):
                c.execute(
                    "INSERT INTO import_queue(torrent_name, status) VALUES(?,?)",
                    ("rel.torrent", status)
                )

    findings = _diagnose(fresh_db)
    by_code = _by_code(findings)
    assert by_code["import_queue_failed"].count == 2
    assert by_code["import_queue_importing"].count == 1
    assert by_code["import_queue_partial"].count == 1
    # 'done' state should not produce any finding
    assert "import_queue_done" not in by_code


# ───────────────────────── orphan detection ──────────────────────────────────

def test_diagnose_detects_orphan_volumes(fresh_db):
    """Volume whose series_id points to a missing series → critical orphan."""
    with sqlite3.connect(fresh_db) as c:
        # Don't insert series(id=99) — volume references it as orphan.
        c.execute("INSERT INTO volumes(series_id, volume_num, status) VALUES(99, 1, 'wanted')")

    findings = _diagnose(fresh_db)
    by_code = _by_code(findings)
    assert by_code["orphan_volumes"].count == 1
    assert by_code["orphan_volumes"].severity == "critical"


def test_diagnose_detects_orphan_chapters(fresh_db):
    """Chapter whose volume_id points to a missing volume → critical orphan."""
    with sqlite3.connect(fresh_db) as c:
        # Chapter references volume 999 which doesn't exist.
        # series_id NOT NULL — provide one but make the volume reference orphaned.
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute("INSERT INTO chapters(series_id, volume_id, chapter_num, status) VALUES(1, 999, 1.0, 'wanted')")

    findings = _diagnose(fresh_db)
    by_code = _by_code(findings)
    assert by_code["orphan_chapters"].count == 1
    assert by_code["orphan_chapters"].severity == "critical"


# ───────────────────────── api_key safety check ──────────────────────────────

def test_diagnose_critical_when_api_key_blank(fresh_db):
    """A blank api_key means /api routes 401 everything — critical regression."""
    with sqlite3.connect(fresh_db) as c:
        c.execute("UPDATE settings SET value='' WHERE key='api_key'")

    findings = _diagnose(fresh_db)
    crits = [f for f in findings if f.severity == "critical"]
    assert any(f.code == "api_key_blank" for f in crits), (
        f"expected api_key_blank critical; got {[f.code for f in crits]}"
    )


def test_diagnose_no_critical_when_api_key_present(fresh_db):
    """init_db() seeds an api_key on a fresh DB — no critical finding expected."""
    findings = _diagnose(fresh_db)
    crits = [f for f in findings if f.severity == "critical"]
    assert crits == [], f"expected no criticals on fresh DB; got: {crits}"


# ───────────────────────── exit code semantics ───────────────────────────────

def test_main_exits_zero_on_clean_db(fresh_db):
    """CLI must exit 0 on a healthy DB so operator scripts don't false-alarm."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))
    import verify_e2e
    rc = verify_e2e.main(["verify_e2e.py", fresh_db])
    assert rc == 0


def test_main_exits_nonzero_on_critical(fresh_db):
    """CLI must signal critical issues via exit code so CI/cron pipes catch them."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))
    import verify_e2e
    with sqlite3.connect(fresh_db) as c:
        c.execute("UPDATE settings SET value='' WHERE key='api_key'")
    rc = verify_e2e.main(["verify_e2e.py", fresh_db])
    assert rc != 0
