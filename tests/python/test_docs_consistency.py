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


def test_compose_publishes_standard_lan_port_and_docs_show_host_only_option():
    """The public Compose file should work on a LAN without interpolation."""
    compose = _read("docker-compose.yml")
    assert '- "6789:8000"' in compose
    assert "${MANGARR_BIND_ADDRESS" not in compose

    deployment = _read("docs/deployment.md")
    assert '"127.0.0.1:6789:8000"' in deployment
    assert "published directly to the internet" in deployment


def test_public_install_does_not_require_env_file():
    """Self-hosters configure the tracked Compose example directly."""
    assert not (REPO_ROOT / ".env.example").exists()
    assert "cp .env.example" not in _read("README.md")
    assert "${" not in _read("docker-compose.yml")


def test_public_compose_is_host_neutral_and_uses_release_image():
    """The tracked Compose file must be safe to publish unchanged."""
    compose = _read("docker-compose.yml")
    assert "ghcr.io/kha-kis/manga-arr:latest" in compose
    assert "- ./config:/config" in compose
    assert "- ./data:/data" in compose
    assert 'user: "1000:1000"' in compose
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
        assert "Create administrator" in text
        assert ".mangarr-setup-token" not in text
        assert "first browser" in text.lower()
    assert "python /app/auth_cli.py reset-admin --yes" in deployment


def test_public_docs_cover_versioned_upgrade_and_rollback():
    readme = _read("README.md")
    deployment = _read("docs/deployment.md")
    releases = _read("docs/releases.md")
    for marker in (
        "ghcr.io/kha-kis/manga-arr:latest",
        "docker compose pull",
        "docker compose up -d",
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
        '"6789:8000"',             # LAN example
        "reverse proxy",           # pattern 3
        "Security Checklist",      # checklist section
        "API key",                 # referenced from H2
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
        'user: "1001:1001"',                 # the exact override example
        "Container User",                    # the section exists
    ]:
        assert marker in doc, \
            f"docs/deployment.md missing UID-override marker: {marker!r}"


def test_deployment_doc_documents_proxy_env_guidance():
    """Outbound proxy setup is documented as optional Compose environment."""
    doc = _read("docs/deployment.md")
    for marker in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        assert marker in doc, \
            f"docs/deployment.md missing proxy env marker: {marker!r}"
    assert "optional container environment" in doc


def test_compose_shows_user_override_pattern():
    """Self-hosters must be able to match bind-directory ownership."""
    compose = _read("docker-compose.yml")
    assert 'user: "1000:1000"' in compose, \
        "docker-compose.yml should show a directly editable non-root UID/GID"


def test_gitignore_excludes_local_compose_overrides_and_env():
    """Untracked local Compose configuration must stay out of source control."""
    gi = _read(".gitignore")
    lines = [ln.strip() for ln in gi.splitlines()]
    assert ".env" in lines, ".gitignore should exclude .env"
    assert "docker-compose.override.yml" in lines, \
        "host-specific Compose overrides must never be committed"
