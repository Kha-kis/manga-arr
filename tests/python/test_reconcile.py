"""Tests for app/reconcile.py — read-only report + dry-run repair planner.

Critical invariants:
  - plan() never mutates the DB
  - the connection is opened in read-only URI mode
  - ghost chapters always carry requires_manual_review=True
  - orphan rows always carry severity='critical' and requires_manual_review=True
  - stuck-grabbed volumes that ARE in import_queue do not get an auto-reset
    proposal — only review
  - stuck-grabbed volumes that are NOT in import_queue propose the same
    field clear the live app's check_download_status applies
  - 'partial' actions are always high risk + manual (some files may be in lib)
  - failed import_queue rows are always manual review
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
import conftest  # noqa: F401


def _import_reconcile():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))
    import reconcile
    return reconcile


@pytest.fixture
def fresh_db():
    import main, shared, security
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close(); os.unlink(db.name)
    key_dir = tempfile.mkdtemp(prefix="mangarr-rec-keys-")

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


def _by_action(actions):
    """Group by (action, target) for assertion convenience."""
    return {(a.action, a.target): a for a in actions}


# ─────────────────── invariants: never mutates ───────────────────────────────

def test_plan_does_not_mutate_db(fresh_db):
    """Running plan() must not change any row, even when it has work to do."""
    reconcile = _import_reconcile()
    # Seed one of every kind of residue.
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO volumes(series_id, volume_num, status, grabbed_at, "
            " download_id, torrent_name, indexer, protocol, source_url) "
            " VALUES(?,?,?, datetime('now', '-5 days'), ?, ?, ?, ?, ?)",
            (1, 1, "grabbed", "abc", "name.torrent", "MockIdx", "torrent",
             "magnet:?xt=urn:btih:" + "a"*40)
        )
        c.execute(
            "INSERT INTO chapters(series_id, volume_id, chapter_num, status,"
            " quality, import_path) VALUES(?,?,?,?,?,?)",
            (1, None, 1.0, "downloaded", None, None)
        )
        c.execute("INSERT INTO import_queue(torrent_name, status, created_at)"
                  " VALUES(?,?, datetime('now', '-1 day'))", ("rel.torrent", "failed"))

    # Snapshot every row from the residue tables.
    def _snapshot():
        with sqlite3.connect(fresh_db) as c:
            return {
                "volumes":  list(c.execute("SELECT * FROM volumes ORDER BY id")),
                "chapters": list(c.execute("SELECT * FROM chapters ORDER BY id")),
                "iq":       list(c.execute("SELECT * FROM import_queue ORDER BY id")),
            }

    before = _snapshot()
    actions = reconcile.plan(fresh_db)
    after = _snapshot()

    assert actions, "fixture should have produced at least one action"
    assert before == after, (
        "plan() mutated the DB — invariant violated. Diff:\n"
        f"before={before!r}\nafter={after!r}"
    )


def test_plan_uses_readonly_connection(fresh_db):
    """The connection helper must open in read-only mode. Direct write
    attempts after the helper opens must raise OperationalError, proving
    the URI flag actually engaged."""
    reconcile = _import_reconcile()
    conn = reconcile._ro_connect(fresh_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO settings(key, value) VALUES('x', 'y')")
    finally:
        conn.close()


def test_plan_returns_empty_on_clean_db(fresh_db):
    """Fresh DB has no residue → no actions."""
    reconcile = _import_reconcile()
    assert reconcile.plan(fresh_db) == []


# ─────────────────── stuck grabbed volume ────────────────────────────────────

def test_stuck_grabbed_not_in_queue_proposes_reset(fresh_db):
    """The exact field-clear set must match what check_download_status applies
    in main.py. Drift here = drift in operator expectations."""
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, grabbed_at,"
            " download_id, source_url, torrent_name, indexer, protocol, client,"
            " release_group, import_path) "
            " VALUES(7, 1, 2, 'grabbed', datetime('now', '-5 days'), 'dlid',"
            " 'magnet:x', 'name.torrent', 'idx', 'torrent', 'qbit', 'group', NULL)"
        )

    actions = reconcile.plan(fresh_db)
    by_target = {a.target: a for a in actions}
    a = by_target.get(("volumes", 7))
    assert a is not None
    assert a.action == "reset_to_wanted"
    assert a.requires_manual_review is False
    assert a.risk == "low"
    # The mutation must blank exactly the columns the live auto-reset blanks.
    assert a.would_mutate == {
        "status":         "wanted",
        "grabbed_at":     None,
        "download_id":    None,
        "source_url":     None,
        "torrent_name":   None,
        "indexer":        None,
        "protocol":       None,
        "client":         None,
        "release_group":  None,
        "import_path":    None,
    }


def test_stuck_grabbed_in_active_queue_requires_review(fresh_db):
    """If an import_queue row is still pending/partial/importing for the
    volume's download_id, the import may complete — don't propose reset."""
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, grabbed_at, download_id)"
            " VALUES(8, 1, 2, 'grabbed', datetime('now', '-5 days'), 'dl-active')"
        )
        c.execute(
            "INSERT INTO import_queue(download_id, status, created_at)"
            " VALUES('dl-active', 'pending', datetime('now', '-5 days'))"
        )

    actions = reconcile.plan(fresh_db)
    vol_actions = [a for a in actions if a.target == ("volumes", 8)]
    assert len(vol_actions) == 1
    a = vol_actions[0]
    assert a.action == "leave_pending_import"
    assert a.requires_manual_review is True
    assert a.would_mutate == {}


def test_recently_grabbed_volume_is_not_proposed(fresh_db):
    """Volumes grabbed within the threshold must not appear in the plan."""
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, grabbed_at)"
            " VALUES(9, 1, 1, 'grabbed', datetime('now', '-1 hour'))"
        )

    actions = reconcile.plan(fresh_db)
    assert not any(a.target == ("volumes", 9) for a in actions)


# ─────────────────── ghost chapter ───────────────────────────────────────────

def test_ghost_chapter_always_requires_manual_review(fresh_db):
    """We can't tell from DB state alone whether the file is missing or just
    metadata is missing. Always manual."""
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO chapters(id, series_id, chapter_num, status, quality, import_path)"
            " VALUES(33, 1, 5.0, 'downloaded', NULL, NULL)"
        )

    actions = reconcile.plan(fresh_db)
    by_t = _by_action(actions)
    a = by_t[("revert_ghost_chapter_to_wanted", ("chapters", 33))]
    assert a.requires_manual_review is True
    assert a.would_mutate == {"status": "wanted"}
    assert a.risk == "medium"


def test_complete_downloaded_chapter_is_not_proposed(fresh_db):
    """Chapters with both quality and import_path are healthy — no action."""
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO chapters(id, series_id, chapter_num, status, quality, import_path)"
            " VALUES(50, 1, 1.0, 'downloaded', 'WEB-DL', '/data/x.cbz')"
        )

    actions = reconcile.plan(fresh_db)
    assert not any(a.target == ("chapters", 50) for a in actions)


# ─────────────────── stale importing / partial / failed queue ────────────────

def test_stale_importing_with_no_review_files_is_low_risk(fresh_db):
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute(
            "INSERT INTO import_queue(id, torrent_name, status, created_at)"
            " VALUES(11, 'name', 'importing', datetime('now', '-1 day'))"
        )

    actions = reconcile.plan(fresh_db)
    by_t = _by_action(actions)
    a = by_t[("revert_to_failed_for_retry", ("import_queue", 11))]
    assert a.requires_manual_review is False
    assert a.risk == "low"
    assert a.would_mutate == {"status": "failed"}


def test_stale_importing_with_review_files_requires_manual(fresh_db):
    """needs_review files carry user decisions — never auto-revert."""
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute(
            "INSERT INTO import_queue(id, torrent_name, status, created_at)"
            " VALUES(12, 'name', 'importing', datetime('now', '-1 day'))"
        )
        c.execute(
            "INSERT INTO import_queue_files(queue_id, src_path, status)"
            " VALUES(12, '/x.cbz', 'needs_review')"
        )

    actions = reconcile.plan(fresh_db)
    by_t = _by_action(actions)
    a = by_t[("revert_to_failed_for_retry", ("import_queue", 12))]
    assert a.requires_manual_review is True
    assert a.risk == "medium"


def test_partial_status_is_always_high_risk_and_manual(fresh_db):
    """Partial means some files MAY already be in the library — never auto."""
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute(
            "INSERT INTO import_queue(id, torrent_name, status, created_at)"
            " VALUES(20, 'name', 'partial', datetime('now', '-1 day'))"
        )

    actions = reconcile.plan(fresh_db)
    by_t = _by_action(actions)
    a = by_t[("revert_to_failed_for_retry", ("import_queue", 20))]
    assert a.requires_manual_review is True
    assert a.risk == "high"


def test_failed_queue_row_is_always_manual_review_only(fresh_db):
    """We never auto-retry a failed import — operator decides retry/blocklist/delete."""
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute(
            "INSERT INTO import_queue(id, torrent_name, status, created_at)"
            " VALUES(30, 'name', 'failed', datetime('now', '-1 day'))"
        )

    actions = reconcile.plan(fresh_db)
    by_t = _by_action(actions)
    a = by_t[("review_failed_import", ("import_queue", 30))]
    assert a.action == "review_failed_import"
    assert a.would_mutate == {}
    assert a.requires_manual_review is True


# ─────────────────── orphans always critical, never auto ─────────────────────

def test_orphan_volume_is_critical_and_manual(fresh_db):
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status)"
            " VALUES(40, 99, 1, 'wanted')"  # series_id=99 doesn't exist
        )

    actions = reconcile.plan(fresh_db)
    by_t = _by_action(actions)
    a = by_t[("manual_review_orphan_volume", ("volumes", 40))]
    assert a.severity == "critical"
    assert a.requires_manual_review is True
    assert a.would_mutate == {}


def test_orphan_chapter_is_critical_and_manual(fresh_db):
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO chapters(id, series_id, volume_id, chapter_num, status)"
            " VALUES(50, 1, 999, 1.0, 'wanted')"  # volume 999 doesn't exist
        )

    actions = reconcile.plan(fresh_db)
    by_t = _by_action(actions)
    a = by_t[("manual_review_orphan_chapter", ("chapters", 50))]
    assert a.severity == "critical"
    assert a.requires_manual_review is True


# ─────────────────── CLI ─────────────────────────────────────────────────────

def test_cli_report_subcommand_works_on_clean_db(fresh_db, capsys):
    reconcile = _import_reconcile()
    rc = reconcile.main(["reconcile.py", "report", fresh_db])
    captured = capsys.readouterr()
    assert rc == 0
    assert "END-TO-END STATE MACHINE VERIFICATION" in captured.out


def test_cli_plan_subcommand_works_on_clean_db(fresh_db, capsys):
    reconcile = _import_reconcile()
    rc = reconcile.main(["reconcile.py", "plan", fresh_db])
    captured = capsys.readouterr()
    assert rc == 0
    assert "No reconciliation actions proposed" in captured.out


def test_cli_plan_prints_dry_run_disclaimer(fresh_db, capsys):
    """Operator must see 'no rows were modified' so they know to apply manually."""
    reconcile = _import_reconcile()
    with sqlite3.connect(fresh_db) as c:
        c.execute("INSERT INTO series(id, title, search_pattern) VALUES(1, 'X', 'X')")
        c.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status, grabbed_at)"
            " VALUES(1, 1, 1, 'grabbed', datetime('now', '-5 days'))"
        )

    rc = reconcile.main(["reconcile.py", "plan", fresh_db])
    captured = capsys.readouterr()
    assert rc == 0
    assert "DRY RUN" in captured.out
    assert "No rows were modified" in captured.out


def test_cli_unknown_command_returns_usage(capsys):
    reconcile = _import_reconcile()
    rc = reconcile.main(["reconcile.py", "wat"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage:" in captured.err
