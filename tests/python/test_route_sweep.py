"""Auto-derived route sweep — release gate.

Walks every GET route registered on the FastAPI app and asserts that:
  - HTML pages return 200 (or an expected redirect)
  - the response renders without raising a template UndefinedError
  - the body has non-trivial content

This catches the class of bug where a router is included but its template
context is incomplete (the `/indexers` regression: secret_health was added
to the template but not to the route context, producing a 500
UndefinedError that no test caught until a manual browser sweep).

The sweep uses TestClient against an in-process FastAPI app pointed at a
fresh temp DB (via conftest's /config redirect + per-test DB_PATH override).
It does NOT require a running container or live DB.

Routes that take path parameters or are otherwise not safely GETable
without setup are filtered out with a documented reason. The point is to
catch new untested top-level pages automatically — every newly registered
parameter-free GET HTML route flows through this gate without test edits.
"""
import os
import re
import sqlite3
import sys
import tempfile

import pytest


# Skiplist with reasons. Anything new not in here gets exercised.
_SKIP_PATHS = {
    "/api/queue-events":  "SSE stream — would block",
    "/static":             "StaticFiles mount, not a renderable page",
    "/covers":             "StaticFiles mount, not a renderable page",
}

# Paths that legitimately return a non-200 status for an unauthenticated /
# unconfigured GET. Pin them so a regression to "now returns 500" is caught.
_EXPECTED_NON_200 = {
    # path: set of acceptable status codes
}


def _is_renderable_html_get(route) -> bool:
    """True iff this is a GET route that should render HTML without args."""
    from fastapi.routing import APIRoute
    if not isinstance(route, APIRoute):
        return False
    if "GET" not in route.methods:
        return False
    # Skip parameterised routes — they need real IDs to render. The sweep
    # is a top-level page check, not a deep crawl.
    if "{" in route.path:
        return False
    # Skip /api/* — those are JSON endpoints behind the API-key middleware
    # and are exercised by their own targeted tests.
    if route.path.startswith("/api/"):
        return False
    if any(route.path.startswith(p) for p in _SKIP_PATHS):
        return False
    return True


@pytest.fixture(scope="module")
def app_with_temp_db():
    """Boot main:app against a fresh temp DB and seed an api_key + csrf.

    Module-scoped so the 60+ route hits share one app instance; each
    request is independent.
    """
    import main, shared

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.unlink(tmp.name)

    orig_main_db = main.DB_PATH
    orig_shared_db = shared.DB_PATH
    main.DB_PATH = tmp.name
    shared.DB_PATH = tmp.name

    try:
        main.init_db()
        main.load_config()
        main.ensure_api_key()
        yield main.app
    finally:
        main.DB_PATH = orig_main_db
        shared.DB_PATH = orig_shared_db
        for ext in ("", "-wal", "-shm"):
            p = tmp.name + ext
            if os.path.exists(p):
                os.unlink(p)


def _gather_renderable_routes(app):
    return [r for r in app.routes if _is_renderable_html_get(r)]


def test_route_sweep_finds_routes(app_with_temp_db):
    """Sanity check: the sweep must discover a non-trivial number of pages.
    Guards against a future refactor that skips everything."""
    routes = _gather_renderable_routes(app_with_temp_db)
    assert len(routes) >= 15, (
        f"Expected at least 15 renderable GET routes, found {len(routes)}: "
        f"{[r.path for r in routes]}"
    )


def test_no_skiplist_drift(app_with_temp_db):
    """Any path on the skiplist must still exist on the app. If a route is
    deleted, remove it from _SKIP_PATHS too — otherwise we silently lose
    coverage if a renamed path comes back later."""
    all_paths = {r.path for r in app_with_temp_db.routes if hasattr(r, "path")}
    stale = [p for p in _SKIP_PATHS if not any(ap.startswith(p) for ap in all_paths)]
    assert not stale, f"_SKIP_PATHS contains paths no longer registered: {stale}"


@pytest.mark.parametrize("path", [
    # Hard-coded representative pages — if parametrize-via-fixture ever
    # silently empties out, these still exercise the critical flows.
    "/",
    "/indexers",
    "/download-clients",
    "/notifications",
    "/settings",
    "/health",
])
def test_critical_pages_render(app_with_temp_db, path):
    """Pin the operator-critical top-level pages explicitly."""
    from fastapi.testclient import TestClient
    client = TestClient(app_with_temp_db, follow_redirects=False)
    r = client.get(path)
    assert r.status_code in (200, 301, 302, 303, 307, 308), (
        f"{path}: expected 200/redirect, got {r.status_code}: {r.text[:300]!r}"
    )
    if r.status_code == 200:
        assert r.headers.get("content-type", "").startswith("text/html"), (
            f"{path}: expected HTML response, got {r.headers.get('content-type')!r}"
        )
        assert len(r.content) > 200, f"{path}: response body suspiciously small"


def test_full_sweep_renders_every_top_level_page(app_with_temp_db):
    """Walk every parameter-free GET HTML route. Any new page added to a
    router gets exercised automatically — no test edit required."""
    from fastapi.testclient import TestClient
    client = TestClient(app_with_temp_db, follow_redirects=False)
    routes = _gather_renderable_routes(app_with_temp_db)
    failures = []
    for r in routes:
        try:
            resp = client.get(r.path)
        except Exception as e:
            failures.append((r.path, "exception", repr(e)))
            continue
        expected = _EXPECTED_NON_200.get(r.path, {200, 301, 302, 303, 307, 308})
        if resp.status_code not in expected:
            body = resp.text[:400] if resp.status_code >= 500 else ""
            failures.append((r.path, resp.status_code, body))
            continue
        # For 200 HTML responses, sanity-check it's not an empty shell.
        if resp.status_code == 200:
            ct = resp.headers.get("content-type", "")
            if ct.startswith("text/html") and len(resp.content) < 100:
                failures.append((r.path, "tiny-body", repr(resp.text)))
    assert not failures, "Route sweep failures:\n" + "\n".join(
        f"  {p}  [{status}]  {body}" for p, status, body in failures
    )


def test_no_jinja_undefined_errors(app_with_temp_db):
    """Specifically catch the /indexers class of bug: a 500 caused by a
    template referencing a context key the route forgot to populate.

    A jinja2.exceptions.UndefinedError surfaces as an HTTP 500. We assert
    no top-level page returns 500 — that's the regression signature.
    """
    from fastapi.testclient import TestClient
    client = TestClient(app_with_temp_db, follow_redirects=False)
    routes = _gather_renderable_routes(app_with_temp_db)
    five_hundreds = []
    for r in routes:
        try:
            resp = client.get(r.path)
        except Exception as e:
            five_hundreds.append((r.path, f"exception: {e!r}"))
            continue
        if resp.status_code == 500:
            # Capture a marker if it looks like an Undefined error; the
            # body in DEBUG mode includes the exception class name.
            marker = "Undefined" if "Undefined" in resp.text else "500"
            five_hundreds.append((r.path, marker))
    assert not five_hundreds, (
        "Pages returned HTTP 500 (likely template/context bug):\n"
        + "\n".join(f"  {p}  {marker}" for p, marker in five_hundreds)
    )
