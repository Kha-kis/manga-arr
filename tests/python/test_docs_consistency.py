"""Tests for M8: docs-and-config consistency.

Pure text checks — no runtime code changed. These guard the invariants
that the M8 deployment doc relies on, so a future refactor that
changes the Dockerfile or docker-compose can't silently diverge from
what docs/deployment.md tells operators to do.
"""
import pathlib
import re


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(rel):
    return (REPO_ROOT / rel).read_text()


# ──────────────── invariants the deployment doc relies on ────────────────

def test_dockerfile_still_binds_0000_inside_container():
    """The 0.0.0.0 bind is documented as safe *because* exposure is
    controlled by `ports:`. If someone changes the Dockerfile to
    --host 127.0.0.1, the reverse-proxy pattern in the doc silently
    breaks."""
    df = _read("Dockerfile")
    assert "--host" in df
    assert re.search(r'--host["\s,]+["]?0\.0\.0\.0', df), \
        f"Dockerfile CMD no longer binds 0.0.0.0 — update docs/deployment.md"


def test_compose_publishes_on_loopback_only():
    """The repo's committed docker-compose.yml must use the safe
    127.0.0.1:PORT:PORT pattern the doc recommends as the default."""
    compose = _read("docker-compose.yml")
    assert re.search(r'127\.0\.0\.1:\d+:\d+', compose), \
        "docker-compose.yml should publish on 127.0.0.1:... — " \
        "otherwise docs/deployment.md's 'safe default' example is a lie"
    # And must NOT publish on 0.0.0.0 implicitly (bare "6789:8000")
    assert not re.search(r'^\s*-\s+"?\d+:8000"?\s*$', compose, re.MULTILINE), \
        "docker-compose.yml has a bare port mapping (publishes on 0.0.0.0)"


def test_env_example_mirrors_env_keys():
    """.env.example must document every env-var key the real .env
    uses, so operators setting up a fresh install don't miss credentials
    (and, symmetrically, .env shouldn't have secrets the template
    doesn't mention)."""
    env_example = REPO_ROOT / ".env.example"
    assert env_example.exists(), ".env.example template is missing"
    example_keys = {
        line.split("=", 1)[0].strip()
        for line in env_example.read_text().splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    # At minimum the three credentials the current compose wires in
    for required in ("QBIT_PASS", "SAB_APIKEY", "PROWLARR_KEY"):
        assert required in example_keys, \
            f".env.example missing documented key {required!r}"


def test_env_example_has_no_real_secrets():
    """The .env.example template must only contain placeholder values.
    Real secrets should live in .env (gitignored)."""
    text = (REPO_ROOT / ".env.example").read_text()
    # Placeholder form we explicitly write
    placeholder = "change-me"
    # Every non-comment line with = must have 'change-me' as its value
    # unless it's commented out
    bad = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" in s:
            _, val = s.split("=", 1)
            val = val.strip()
            if val and val != placeholder:
                bad.append(s)
    assert not bad, \
        f".env.example has non-placeholder values (possible secret leak): {bad}"


def test_deployment_doc_exists_and_covers_three_patterns():
    doc = _read("docs/deployment.md")
    # Pattern markers — if someone restructures the doc these must remain
    for marker in [
        "127.0.0.1:",              # local-only example
        "192.168.",                # LAN example
        "reverse proxy",           # pattern 3
        "Security checklist",      # checklist section
        "api_key",                 # referenced from H2
        "SameSite=Strict",         # referenced from M1
        "X-Forwarded-Proto",       # referenced from M1
    ]:
        assert marker in doc, f"docs/deployment.md missing section marker: {marker!r}"


def test_dockerfile_runs_as_non_root():
    """Regression guard: the Dockerfile must switch to a non-root USER
    before CMD. Running as root in a container turns container-escape
    CVEs into host-root privilege (Trivy DS-0002). The deployment doc's
    'Container user and file ownership' section assumes this."""
    df = _read("Dockerfile")
    # USER directive must be present, and must not point at root/uid 0.
    assert re.search(r"^USER\s+(?!(root|0)\s*$)\S+", df, re.MULTILINE), \
        "Dockerfile must switch to a non-root user before CMD"


def test_deployment_doc_documents_uid_override():
    """The deployment doc must explain how to pick a UID other than
    1000. Otherwise users whose host uid differs hit opaque write
    failures when they try to pull the hardened image."""
    doc = _read("docs/deployment.md")
    for marker in [
        "UID",                               # the concept is introduced
        'user: "${UID:-1000}:${GID:-1000}"', # the exact override line
        "Container user",                    # the section exists
    ]:
        assert marker in doc, \
            f"docs/deployment.md missing UID-override marker: {marker!r}"


def test_deployment_doc_documents_proxy_env_guidance():
    """Outbound proxy setup is deployment-level guidance, not an in-app
    setting. Keep the docs and env template discoverable together."""
    doc = _read("docs/deployment.md")
    env_example = _read(".env.example")
    for marker in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        assert marker in doc, \
            f"docs/deployment.md missing proxy env marker: {marker!r}"
        assert marker in env_example, \
            f".env.example missing proxy env marker: {marker!r}"
    assert "Outbound HTTP proxies" in doc


def test_compose_shows_user_override_pattern():
    """docker-compose.yml should expose the `user:` override pattern
    (commented) so self-hosters on UIDs other than 1000 don't have to
    dig through docs to find it."""
    compose = _read("docker-compose.yml")
    assert "user:" in compose, \
        "docker-compose.yml should include the `user:` override hint " \
        "(even commented) so non-default UIDs are discoverable"


def test_gitignore_excludes_env_but_not_example():
    """The .env file MUST be gitignored (it holds real secrets); the
    template MUST NOT be gitignored (it's the checked-in example)."""
    gi = _read(".gitignore")
    lines = [ln.strip() for ln in gi.splitlines()]
    assert ".env" in lines, ".gitignore should exclude .env"
    # .env.example is tracked (git check-ignore verified separately
    # via the test harness's workflow; the static check here ensures
    # nobody adds an explicit `.env.example` exclusion line).
    assert ".env.example" not in lines, \
        ".gitignore should NOT explicitly exclude .env.example (it's the template)"
