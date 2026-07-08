"""Live integration tests — skipped by default, opt-in via env flags.

Each test is gated by a per-provider env var. Without the flag set the test
skips with a clear reason so CI never makes outbound calls. With the flag,
the test makes a single read-only request against the configured upstream
(version/status/caps/system endpoints — never a search-and-grab).

Usage examples:
  PROWLARR_LIVE=1 PROWLARR_URL=http://prowlarr:9696/prowlarr \
    PROWLARR_API_KEY=xxx pytest tests/python/test_live_integrations.py -v

  QBITTORRENT_LIVE=1 QBIT_HOST=http://10.200.200.2:10048 \
    QBIT_USER=khak1s QBIT_PASS=xxx pytest tests/python/test_live_integrations.py -v

These never run from the standard PR/release gate. They exist so the
operator can validate real upstream connectivity before tagging a release
without having to remember the curl incantations.
"""

import os

import pytest


def _skip_unless(flag: str, reason: str | None = None):
    """Decorator: skip with a clear reason unless `flag` is truthy in env."""
    val = os.environ.get(flag, "")
    if val and val.lower() not in ("0", "false", "no"):
        return pytest.mark.skipif(False, reason="")
    return pytest.mark.skip(reason or f"set {flag}=1 to run")


# ───────────────────────── Prowlarr ──────────────────────────────────────────


@_skip_unless(
    "PROWLARR_LIVE",
    "Prowlarr live test (set PROWLARR_LIVE=1 + PROWLARR_URL + PROWLARR_API_KEY)",
)
def test_prowlarr_system_status():
    """GET /api/v1/system/status returns 200 with a version field."""
    import httpx

    url = os.environ.get("PROWLARR_URL", "").rstrip("/")
    key = os.environ.get("PROWLARR_API_KEY", "")
    assert url and key, "PROWLARR_URL and PROWLARR_API_KEY must be set"
    url = url.rstrip("/")
    # Detect urlBase from /api/v1/system/status, following redirects
    if not url.endswith("/prowlarr"):
        r = httpx.get(
            f"{url}/prowlarr/api/v1/system/status",
            headers={"X-Api-Key": key},
            timeout=10,
            follow_redirects=True,
        )
    else:
        r = httpx.get(
            f"{url}/api/v1/system/status",
            headers={"X-Api-Key": key},
            timeout=10,
            follow_redirects=True,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "version" in body, f"unexpected response: {body!r}"


# ───────────────────────── qBittorrent ───────────────────────────────────────


@_skip_unless(
    "QBITTORRENT_LIVE",
    "qBittorrent live test (set QBITTORRENT_LIVE=1 + QBIT_HOST + QBIT_USER + QBIT_PASS)",
)
def test_qbittorrent_auth_and_version():
    """POST /api/v2/auth/login then GET /api/v2/app/version. Read-only.

    Does NOT add any torrents. Asserts the auth flow and version endpoint
    return successfully.
    """
    import httpx

    host = os.environ.get("QBIT_HOST", "").rstrip("/")
    user = os.environ.get("QBIT_USER", "")
    pw = os.environ.get("QBIT_PASS", "")
    assert host and user and pw, "QBIT_HOST + QBIT_USER + QBIT_PASS required"
    with httpx.Client(timeout=10) as c:
        r = c.post(f"{host}/api/v2/auth/login", data={"username": user, "password": pw})
        assert r.status_code == 200, r.text
        assert "Ok" in r.text, f"auth fail: {r.text!r}"
        r2 = c.get(f"{host}/api/v2/app/version", cookies=r.cookies)
        assert r2.status_code == 200
        assert r2.text.strip(), "empty version response"


# ───────────────────────── SABnzbd ───────────────────────────────────────────


@_skip_unless(
    "SABNZBD_LIVE", "SABnzbd live test (set SABNZBD_LIVE=1 + SAB_HOST + SAB_API_KEY)"
)
def test_sabnzbd_queue_endpoint():
    """GET /api?mode=queue returns 200. Read-only — does not modify queue."""
    import httpx

    host = os.environ.get("SAB_HOST", "").rstrip("/")
    key = os.environ.get("SAB_API_KEY", "")
    assert host and key, "SAB_HOST + SAB_API_KEY required"
    r = httpx.get(
        f"{host}/api",
        params={"mode": "queue", "output": "json", "apikey": key},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "queue" in body, f"unexpected response: {body!r}"


# ───────────────────────── MangaDex ──────────────────────────────────────────


@_skip_unless("MANGADEX_LIVE", "MangaDex live test (set MANGADEX_LIVE=1)")
def test_mangadex_ping():
    """MangaDex /ping is the official liveness check. No auth, no rate burn."""
    import httpx

    r = httpx.get("https://api.mangadex.org/ping", timeout=10)
    assert r.status_code == 200, r.text
    # MangaDex returns the literal string "pong"
    assert "pong" in r.text.lower()


# ───────────────────────── Suwayomi ──────────────────────────────────────────


@_skip_unless(
    "SUWAYOMI_LIVE", "Suwayomi live test (set SUWAYOMI_LIVE=1 + SUWAYOMI_URL)"
)
def test_suwayomi_about():
    """GET /api/v1/about returns app metadata. No auth needed by default."""
    import httpx

    url = os.environ.get("SUWAYOMI_URL", "").rstrip("/")
    assert url, "SUWAYOMI_URL required"
    r = httpx.get(f"{url}/api/v1/about", timeout=10)
    assert r.status_code == 200, r.text
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        body = r.json()
        assert any(k in body for k in ("name", "version", "buildType")), (
            f"unexpected response shape: {body!r}"
        )
    else:
        assert "html" in ct, f"unexpected content-type: {ct}"
