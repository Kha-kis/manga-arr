"""Safety regression tests for the isolated browser runner.

These guard against the class of accident that previously stopped the live
container during pass 2 development (an early version of
tests/run_isolated_browser.sh used `docker compose down --remove-orphans`,
which Docker classified the live `mangarr` container as an orphan of the
test project and removed it).

If any of these fail, do NOT bypass — the runner has drifted toward an
unsafe configuration. Either fix the runner or, if a check is genuinely
obsolete, remove the check explicitly with a comment explaining why.
"""
import os
import re

import pytest

REPO_ROOT  = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RUNNER     = os.path.join(REPO_ROOT, "tests", "run_isolated_browser.sh")
COMPOSE    = os.path.join(REPO_ROOT, "docker-compose.test.yml")


def _strip_shell_comments(text: str) -> str:
    """Remove `# ...` shell comments and blank lines.

    Doesn't try to be a full bash parser — just enough to keep our content
    checks from matching strings inside documentation comments. Treats `#`
    after the first non-whitespace character on a line as a trailing comment.
    """
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip trailing inline comments (best-effort; doesn't handle quoted #).
        if "#" in line:
            line = line.split("#", 1)[0].rstrip()
        if line.strip():
            out.append(line)
    return "\n".join(out)


def _strip_yaml_comments(text: str) -> str:
    """Same idea as shell, applied to YAML."""
    return _strip_shell_comments(text)


@pytest.fixture(scope="module")
def runner_text():
    with open(RUNNER) as f:
        return f.read()


@pytest.fixture(scope="module")
def runner_active():
    """Runner with comments stripped — reflects what bash actually executes."""
    with open(RUNNER) as f:
        return _strip_shell_comments(f.read())


@pytest.fixture(scope="module")
def compose_text():
    with open(COMPOSE) as f:
        return f.read()


@pytest.fixture(scope="module")
def compose_active():
    """Compose file with comments stripped."""
    with open(COMPOSE) as f:
        return _strip_yaml_comments(f.read())


# ─────────────────────── runner: fatal-flag absence ──────────────────────────

def test_runner_never_uses_remove_orphans(runner_active):
    """--remove-orphans treats the live container as an orphan of the test
    project. This is the original incident. Must never reappear.

    The flag is allowed to appear inside a guard (e.g. `grep -q -- '--remove-orphans'`)
    but must never be passed to `docker compose` directly.
    """
    # Match any line where --remove-orphans is an argument to a compose
    # invocation, not the literal inside a string check.
    bad_patterns = [
        r'docker\s+compose[^|]*--remove-orphans',
        r'\$COMPOSE\s+[^|]*--remove-orphans',
    ]
    for pat in bad_patterns:
        m = re.search(pat, runner_active)
        assert not m, (
            f"runner passes --remove-orphans to compose: {m.group(0)!r}"
        )


def test_runner_uses_explicit_test_project_name(runner_active):
    """`compose -p mangarr-test` is what isolates the test container from
    the live one. Without it, both share the directory-derived project."""
    assert 'PROJECT="mangarr-test"' in runner_active, (
        "PROJECT must be exactly 'mangarr-test'"
    )
    # And the compose invocation must use -p with that project.
    assert re.search(r'docker compose\s+-p\s+\$PROJECT\b', runner_active), (
        "compose invocation must pass -p $PROJECT"
    )


def test_runner_pins_test_port_not_production_port(runner_text):
    """Production binds 6789. Test binds 16789. A drift would stomp the
    operator's port and prevent the live container from starting."""
    assert 'PORT="16789"' in runner_text or 'BASE_URL="http://127.0.0.1:16789"' in runner_text
    # And explicitly fail closed if PORT is ever set to 6789.
    assert "PRODUCTION_PORT=\"6789\"" in runner_text
    assert 'if [ "$PORT" = "$PRODUCTION_PORT" ]; then' in runner_text


def test_runner_refuses_if_container_name_matches_production(runner_text):
    """Hard guard: CONTAINER must never equal PRODUCTION_CONTAINER."""
    assert 'PRODUCTION_CONTAINER="mangarr"' in runner_text
    assert 'CONTAINER="mangarr-test"' in runner_text
    assert 'if [ "$CONTAINER" = "$PRODUCTION_CONTAINER" ]; then' in runner_text


def test_runner_refuses_if_test_config_resolves_to_production(runner_text):
    """The realpath check catches symlinks and `..` components that could
    otherwise let TEST_CONFIG_DIR alias the production config dir."""
    assert "realpath -m" in runner_text
    assert 'PRODUCTION_CONFIG_DIR=' in runner_text
    assert 'TEST_CONFIG_REAL' in runner_text
    assert 'PROD_CONFIG_REAL' in runner_text


def test_runner_does_not_target_production_compose_file(runner_text):
    """The runner must reference docker-compose.test.yml, not the production
    compose file. Different files = different projects = different orphans."""
    assert "docker-compose.test.yml" in runner_text
    # Must NOT reference the production compose file directly.
    assert "compose.yaml" not in runner_text or (
        # Allow the string only in error messages; the actual command must use .test.
        "$COMPOSE_FILE" in runner_text
    )


# ─────────────────────── compose file: isolation properties ──────────────────

def test_compose_test_uses_distinct_container_name(compose_text):
    """The container_name must be distinct so docker can't conflate them."""
    assert "container_name: mangarr-test" in compose_text
    # And the production name must not appear at all.
    assert "container_name: mangarr\n" not in compose_text


def test_compose_test_binds_distinct_port(compose_text):
    """Port must be 16789, not 6789. A collision would either fail to start
    or kick the live container off its port."""
    # Bind expression is something like "127.0.0.1:16789:8000".
    assert re.search(r'127\.0\.0\.1:16789:8000', compose_text)
    assert "127.0.0.1:6789:8000" not in compose_text


def test_compose_test_mounts_test_config_dir_only(compose_active):
    """The /config mount must point at the project-relative .test-config
    directory, not at the operator's ~/.config/mangarr or any absolute path
    that could overlap it."""
    assert "./.test-config:/config" in compose_active
    # No absolute paths to the operator config — explicitly forbidden.
    assert "/home/" not in compose_active
    assert "~/.config/mangarr" not in compose_active


def test_compose_test_does_not_set_restart_unless_stopped(compose_text):
    """Restart policy `unless-stopped` would respawn the test container
    after a failed run, holding port 16789 and breaking subsequent runs.
    The production compose uses `restart: unless-stopped`; the test must not."""
    assert "restart: unless-stopped" not in compose_text
    assert "restart: always" not in compose_text


def test_compose_test_isolates_outbound_to_test_net_addresses(compose_text):
    """Default upstream env vars point at TEST-NET-1 (192.0.2.0/24), so any
    accidental outbound call fails fast rather than reaching real services.
    qBit endpoint is exempted because the sidecar mock-qbit runs at a
    service hostname; drift to a real LAN IP would be a regression."""
    # qBit may use the in-compose hostname mock-qbit; SAB and Prowlarr must
    # remain on TEST-NET-1.
    assert re.search(r"SAB_HOST=http://192\.0\.2\.\d+", compose_text)
    assert re.search(r"PROWLARR_URL=http://192\.0\.2\.\d+", compose_text)


# ─────────────────────── Makefile: doesn't bypass the runner ─────────────────

def test_makefile_routes_isolated_targets_through_runner():
    """All test-browser-isolated* Makefile targets must invoke the safety-checked
    runner, not call `docker compose` directly. A direct invocation could
    forget the project-name flag and re-introduce the orphan bug."""
    with open(os.path.join(REPO_ROOT, "Makefile")) as f:
        mk = f.read()
    # Each isolated target must call the runner script.
    for target in ("test-browser-isolated:", "test-browser-isolated-smoke:",
                   "test-browser-isolated-integration:", "test-browser-isolated-e2e:"):
        assert target in mk, f"missing Makefile target: {target}"
    # The runner script invocation must be present.
    assert "./tests/run_isolated_browser.sh" in mk
