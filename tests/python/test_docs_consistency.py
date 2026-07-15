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
        "Dockerfile CMD no longer binds 0.0.0.0 — update docs/deployment.md"


def test_compose_publishes_on_loopback_only():
    """The repo's committed docker-compose.yml must use the safe
    127.0.0.1:PORT:PORT pattern the doc recommends as the default."""
    compose = _read("docker-compose.yml")
    assert "${MANGARR_BIND_ADDRESS:-127.0.0.1}" in compose, \
        "docker-compose.yml should publish on 127.0.0.1:... — " \
        "otherwise docs/deployment.md's 'safe default' example is a lie"
    # And must NOT publish on 0.0.0.0 implicitly (bare "6789:8000")
    assert not re.search(r'^\s*-\s+"?\d+:8000"?\s*$', compose, re.MULTILINE), \
        "docker-compose.yml has a bare port mapping (publishes on 0.0.0.0)"


def test_env_example_documents_public_compose_overrides():
    """Every public deployment control should be discoverable in the template."""
    env_example = REPO_ROOT / ".env.example"
    assert env_example.exists(), ".env.example template is missing"
    example_keys = {
        line.split("=", 1)[0].strip()
        for line in env_example.read_text().splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    for required in (
        "MANGARR_VERSION",
        "MANGARR_BIND_ADDRESS",
        "MANGARR_PORT",
        "MANGARR_UID",
        "MANGARR_GID",
        "MANGARR_CONFIG_PATH",
        "MANGARR_DATA_PATH",
    ):
        assert required in example_keys, \
            f".env.example missing documented key {required!r}"


def test_env_example_has_no_real_secrets():
    """Public deployment files must direct credentials to the encrypted UI."""
    text = (REPO_ROOT / ".env.example").read_text()
    for secret_key in (
        "QBIT_PASS",
        "SAB_APIKEY",
        "PROWLARR_KEY",
        "KOMGA_PASS",
        "MANGARR_SECRET_KEY",
    ):
        assert secret_key not in text, \
            f".env.example should not solicit {secret_key}; configure it in the UI"


def test_public_compose_is_host_neutral_and_uses_release_image():
    """The tracked Compose file must be safe to publish unchanged."""
    compose = _read("docker-compose.yml")
    version = _read("app/VERSION").strip()
    assert f"ghcr.io/kha-kis/manga-arr:${{MANGARR_VERSION:-{version}}}" in compose
    assert f"MANGARR_VERSION={version}" in _read(".env.example")
    assert "${MANGARR_CONFIG_PATH:-./config}:/config" in compose
    assert "${MANGARR_DATA_PATH:-./data}:/data" in compose
    for private_value in (
        "/home/",
        "/opt/manga-arr/app:/app",
        "10.200.200.",
        "khak1s",
        "external: true",
        "build: .",
    ):
        assert private_value not in compose, \
            f"public docker-compose.yml contains host-specific value {private_value!r}"


def test_public_community_and_release_qualification_files_exist():
    required = (
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SUPPORT.md",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "docs/release-qualification.md",
    )
    for path in required:
        assert (REPO_ROOT / path).is_file(), path

    readme = _read("README.md")
    for path in ("CONTRIBUTING.md", "SUPPORT.md", "CODE_OF_CONDUCT.md"):
        assert f"]({path})" in readme


def test_public_install_instructions_protect_config_directory():
    """The documented first boot must match the config permission checklist."""
    for path in ("README.md", "docs/deployment.md"):
        text = _read(path)
        assert "mkdir -p config" in text
        assert "chmod 700 config" in text


def test_public_install_docs_cover_browser_auth_setup_and_recovery():
    readme = _read("README.md")
    deployment = _read("docs/deployment.md")
    for text in (readme, deployment):
        assert "/config/.mangarr-setup-token" in text
    assert "python /app/auth_cli.py reset-admin --yes" in deployment


def test_public_docs_cover_versioned_upgrade_and_rollback():
    readme = _read("README.md")
    deployment = _read("docs/deployment.md")
    releases = _read("docs/releases.md")
    for marker in (
        "MANGARR_VERSION",
        "docker compose pull mangarr",
        "docker compose up -d --no-deps mangarr",
    ):
        assert marker in readme
        assert marker in deployment
    for marker in ("app/VERSION", "Semantic Versioning", "latest"):
        assert marker in releases
    assert "/config/.mangarr-secret-key" in deployment
    assert "Do not point an older image" in deployment


def test_security_policy_uses_private_reporting_and_documents_boundary():
    policy = _read("SECURITY.md")
    assert "/security/advisories/new" in policy
    assert "Do not open a public issue" in policy
    assert "/config" in policy


def test_release_docs_cover_local_billing_safe_gate():
    releases = _read("docs/releases.md")
    assert "make release-local" in releases
    assert "make release-push CONFIRM_RELEASE=" in releases
    assert "only tag-triggered workflow" in releases


def test_system_status_exposes_source_and_license_links():
    status = _read("app/templates/system_status.html")
    assert "https://github.com/Kha-kis/manga-arr" in status
    assert "https://github.com/Kha-kis/manga-arr/blob/master/LICENSE" in status
    assert "AGPL-3.0" in status
    assert "Copyright (C) 2026 Kha-kis" in status
    assert "provided without warranty" in status


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
        "MANGARR_UID=1001",                  # the exact override example
        "MANGARR_GID=1001",
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
        assert marker in _read("docker-compose.yml"), \
            f"docker-compose.yml does not pass {marker!r} to the container"
    assert "Outbound HTTP proxies" in doc


def test_compose_shows_user_override_pattern():
    """Self-hosters must be able to match bind-directory ownership."""
    compose = _read("docker-compose.yml")
    assert 'user: "${MANGARR_UID:-1000}:${MANGARR_GID:-1000}"' in compose, \
        "docker-compose.yml should expose configurable non-root UID/GID defaults"


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
    assert "docker-compose.override.yml" in lines, \
        "host-specific Compose overrides must never be committed"
