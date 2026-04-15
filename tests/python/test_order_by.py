"""Tests for M4: ORDER BY allowlist explicitness.

Two layers:
  1. shared.build_order_by helper — pure function; covers the allowlist
     contract, direction validation, injection payload rejection, and
     caller-misuse guards.
  2. Integration with the one known request-controlled ORDER BY site
     (GET / — library index, `sort` query param) — drives the real
     endpoint via TestClient and confirms the SQL emitted is always
     one of the allowlisted fragments regardless of payload.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


# ───────────────────── build_order_by primitive ─────────────────────

def test_valid_sort_key_returns_mapped_fragment():
    from shared import build_order_by
    allowed = {"title": "title", "added": "added_at DESC"}
    assert build_order_by("added", allowed=allowed, default_key="title") == "added_at DESC"
    assert build_order_by("title", allowed=allowed, default_key="title") == "title"


def test_unknown_sort_key_falls_back_to_default():
    from shared import build_order_by
    allowed = {"title": "title", "added": "added_at DESC"}
    assert build_order_by("bogus", allowed=allowed, default_key="title") == "title"


def test_empty_sort_key_falls_back():
    from shared import build_order_by
    allowed = {"title": "title"}
    assert build_order_by("", allowed=allowed, default_key="title") == "title"
    assert build_order_by(None, allowed=allowed, default_key="title") == "title"


def test_direction_asc_appended():
    from shared import build_order_by
    allowed = {"title": "title", "year": "pub_year"}
    assert build_order_by("year", allowed=allowed, default_key="title", direction="asc") == "pub_year ASC"
    assert build_order_by("year", allowed=allowed, default_key="title", direction="ASC") == "pub_year ASC"


def test_direction_desc_appended():
    from shared import build_order_by
    allowed = {"year": "pub_year"}
    assert build_order_by("year", allowed=allowed, default_key="year", direction="desc") == "pub_year DESC"
    assert build_order_by("year", allowed=allowed, default_key="year", direction="DESC") == "pub_year DESC"


def test_invalid_direction_omits_direction():
    """An invalid direction must NOT end up in SQL. The fragment is
    returned column-only so the DB's natural order applies."""
    from shared import build_order_by
    allowed = {"year": "pub_year"}
    for bad in ["ascending", "random", "1; DROP TABLE", "", None]:
        result = build_order_by("year", allowed=allowed, default_key="year", direction=bad)
        assert result == "pub_year", f"{bad!r} produced {result!r}"


def test_caller_misuse_default_key_not_in_allowed():
    from shared import build_order_by
    with pytest.raises(ValueError, match="default_key"):
        build_order_by("x", allowed={"a": "a"}, default_key="not_there")


def test_injection_payload_never_reaches_sql():
    """Classic SQL injection payloads must never appear in the output
    unless they're in the allowlist (they aren't)."""
    from shared import build_order_by
    allowed = {"title": "title", "added": "added_at DESC"}
    payloads = [
        "title; DROP TABLE series;--",
        "title UNION SELECT password FROM users",
        "'; DELETE FROM series WHERE 1=1;--",
        "title ORDER BY 1",
        "(SELECT password FROM users)",
        "title\nDROP TABLE series",
        "title/**/UNION/**/SELECT",
    ]
    for p in payloads:
        result = build_order_by(p, allowed=allowed, default_key="title")
        # The payload must not appear in the returned string at all.
        assert p not in result, f"payload {p!r} leaked into SQL fragment {result!r}"
        # And the result must be one of the allowlisted values.
        assert result in allowed.values(), \
            f"result {result!r} not in allowlist {list(allowed.values())}"


def test_injection_payload_in_direction_never_reaches_sql():
    """Direction payloads are also confined to {ASC, DESC}."""
    from shared import build_order_by
    allowed = {"title": "title"}
    # Note: "\nDESC" strips to "DESC" → valid → returns "title DESC", so
    # it's NOT in this list. The point of the helper is exactly that
    # canonicalisation: only asc/desc ever reach SQL.
    for p in [
        "asc; DROP TABLE series",
        "desc UNION SELECT",
        "ASC;--",
        "d_e_s_c",
    ]:
        result = build_order_by("title", allowed=allowed, default_key="title", direction=p)
        assert p not in result, f"direction payload {p!r} leaked into {result!r}"
        # Direction was invalid → no direction appended
        assert result == "title", f"expected bare 'title', got {result!r}"


# ───────────────────── integration: library index route ─────────────────────

@pytest.fixture
def index_client(monkeypatch):
    """Real FastAPI app with full init_db; library index at / is the
    one endpoint that wires build_order_by."""
    import main
    import shared
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    monkeypatch.setattr(main, "DB_PATH", tmp.name)
    monkeypatch.setattr(shared, "DB_PATH", tmp.name)
    main.init_db()
    main.load_config()

    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    # Capture every SQL statement the library index emits so we can
    # assert on the ORDER BY shape.
    captured_sql = []
    orig_connect = __import__("sqlite3").connect
    import sqlite3 as _sql
    class _SpyConn(_sql.Connection):
        def execute(self, sql, *a, **kw):
            if "ORDER BY" in sql and "FROM series" in sql and "WHERE" not in sql:
                captured_sql.append(sql)
            return super().execute(sql, *a, **kw)
    def _spy_connect(*args, **kwargs):
        kwargs["factory"] = _SpyConn
        return orig_connect(*args, **kwargs)
    monkeypatch.setattr(_sql, "connect", _spy_connect)

    try:
        yield client, captured_sql
    finally:
        for ext in ("", "-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


def test_library_index_default_sort_is_title(index_client):
    """Hitting / without a sort param must emit ORDER BY title — the
    existing default behavior."""
    client, captured = index_client
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    series_sql = [s for s in captured if s.strip().startswith("SELECT * FROM series ORDER BY")]
    assert series_sql, f"no SELECT FROM series ORDER BY captured; got: {captured}"
    # Default is "title"
    assert any("ORDER BY title" in s for s in series_sql), \
        f"expected ORDER BY title, got: {series_sql}"


def test_library_index_valid_sort_status(index_client):
    client, captured = index_client
    r = client.get("/?sort=status", follow_redirects=False)
    assert r.status_code == 200
    series_sql = [s for s in captured if "SELECT * FROM series ORDER BY" in s]
    assert any("ORDER BY status, title" in s for s in series_sql), \
        f"expected ORDER BY status, title; got: {series_sql}"


def test_library_index_valid_sort_added(index_client):
    client, captured = index_client
    r = client.get("/?sort=added", follow_redirects=False)
    assert r.status_code == 200
    series_sql = [s for s in captured if "SELECT * FROM series ORDER BY" in s]
    assert any("ORDER BY added_at DESC" in s for s in series_sql), \
        f"expected ORDER BY added_at DESC; got: {series_sql}"


def test_library_index_unknown_sort_falls_back(index_client):
    """An unknown sort key must not appear in the SQL; default used."""
    client, captured = index_client
    r = client.get("/?sort=bogus_nonsense", follow_redirects=False)
    assert r.status_code == 200
    series_sql = [s for s in captured if "SELECT * FROM series ORDER BY" in s]
    # The string "bogus_nonsense" must not have leaked into the SQL
    for s in series_sql:
        assert "bogus_nonsense" not in s, \
            f"unknown sort key leaked into SQL: {s!r}"
    # Default is "title"
    assert any("ORDER BY title" in s for s in series_sql)


def test_library_index_injection_payload_rejected(index_client):
    """An SQL-injection payload in the sort param must NOT appear in
    the emitted SQL. The route falls back to the default."""
    client, captured = index_client
    payload = "title; DROP TABLE series;--"
    r = client.get("/", params={"sort": payload}, follow_redirects=False)
    # Must not 500 (the injection is refused, not crashing)
    assert r.status_code == 200
    series_sql = [s for s in captured if "SELECT * FROM series ORDER BY" in s]
    assert series_sql
    for s in series_sql:
        # Neither the full payload nor its dangerous fragment should appear
        assert payload not in s, f"full payload leaked: {s!r}"
        assert "DROP TABLE" not in s.upper(), f"DROP TABLE fragment leaked: {s!r}"
        assert ";" not in s.split("ORDER BY", 1)[1], \
            f"semicolon in ORDER BY clause — injection not blocked: {s!r}"
    # Sanity: series table still exists (injection did NOT execute)
    # by re-running the request.
    r2 = client.get("/", follow_redirects=False)
    assert r2.status_code == 200
