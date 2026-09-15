"""Public release automation invariants."""

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from scripts.release_metadata import image_tags, parse_version, validate_release
from scripts.verify_release_image import forbidden_app_path
from version import APP_VERSION


REPO_ROOT = Path(__file__).resolve().parents[2]
AGPL_SHA256 = "d8a6cc31abc16b6748c7a21f21611f5a1ec33f67d22ca23d7da1c19b95496bee"


def test_public_release_uses_canonical_agpl_only_license():
    license_bytes = (REPO_ROOT / "LICENSE").read_bytes()
    assert hashlib.sha256(license_bytes).hexdigest() == AGPL_SHA256
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "AGPL-3.0-only" in readme
    assert "[GNU Affero General Public License v3.0 only](LICENSE)" in readme


def test_current_release_metadata_is_consistent():
    validate_release(APP_VERSION, f"v{APP_VERSION}")


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.0.0-rc.1", ["ghcr.io/kha-kis/manga-arr:1.0.0-rc.1"]),
        (
            "1.2.3",
            [
                "ghcr.io/kha-kis/manga-arr:1.2.3",
                "ghcr.io/kha-kis/manga-arr:1.2",
                "ghcr.io/kha-kis/manga-arr:1",
                "ghcr.io/kha-kis/manga-arr:latest",
            ],
        ),
    ],
)
def test_release_tag_policy(version, expected):
    assert image_tags(version, "ghcr.io/kha-kis/manga-arr") == expected


@pytest.mark.parametrize("version", ["1", "v1.2.3", "1.2.3.4", "1.2.3-rc.01"])
def test_invalid_versions_fail_closed(version):
    with pytest.raises(ValueError):
        parse_version(version)


def test_release_workflow_is_tag_only_and_pins_actions():
    workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text()
    assert 'tags:\n      - "v*"' in workflow
    assert "pull_request:" not in workflow
    assert "workflow_dispatch:" not in workflow
    assert "packages: write" in workflow
    assert "linux/amd64,linux/arm64" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "Refuse to replace an existing release tag" in workflow
    for line in workflow.splitlines():
        if "uses:" in line:
            ref = line.split("@", 1)[1].split()[0]
            assert len(ref) == 40 and all(ch in "0123456789abcdef" for ch in ref)


def test_every_workflow_pins_action_revisions():
    for workflow_path in (REPO_ROOT / ".github/workflows").glob("*.yml"):
        workflow = workflow_path.read_text()
        for line in workflow.splitlines():
            if "uses:" not in line:
                continue
            ref = line.split("@", 1)[1].split()[0]
            assert len(ref) == 40, f"{workflow_path.name}: {line.strip()}"
            assert all(ch in "0123456789abcdef" for ch in ref), (
                f"{workflow_path.name}: {line.strip()}"
            )


def test_docker_context_is_allowlisted():
    dockerignore = (REPO_ROOT / ".dockerignore").read_text().splitlines()
    assert dockerignore[0] == "**"
    assert "!LICENSE" in dockerignore
    assert "!requirements.txt" in dockerignore
    assert "!bin/mangarr" in dockerignore
    assert "app/*" in dockerignore
    assert "!app/*.py" in dockerignore
    assert "app/test_confirm_flow.py" in dockerignore
    assert "app/verify_e2e.py" in dockerignore
    assert "app/routers/*" in dockerignore
    assert "!app/routers/*.py" in dockerignore
    assert "app/static/*" in dockerignore
    assert "!app/static/*.js" in dockerignore
    assert "app/templates/*" in dockerignore
    assert "!app/templates/*.html" in dockerignore
    assert "app/templates/partials/*" in dockerignore
    assert "!app/templates/partials/*.html" in dockerignore
    assert "!app/VERSION" in dockerignore

    tracked = subprocess.check_output(
        ["git", "ls-files", "app"], cwd=REPO_ROOT, text=True
    ).splitlines()
    for relative in tracked:
        if relative in {"app/test_confirm_flow.py", "app/verify_e2e.py"}:
            continue
        path = Path(relative)
        parts = path.parts
        allowed = (
            (len(parts) == 2 and (path.suffix == ".py" or path.name == "VERSION"))
            or (len(parts) == 3 and parts[1] == "routers" and path.suffix == ".py")
            or (
                len(parts) == 3
                and parts[1] == "static"
                and path.suffix in {".js", ".md"}
            )
            or (
                len(parts) == 3
                and parts[1] == "templates"
                and path.suffix == ".html"
            )
            or (
                len(parts) == 4
                and parts[1:3] == ("templates", "partials")
                and path.suffix == ".html"
            )
        )
        assert allowed, relative


@pytest.mark.parametrize(
    "path",
    [
        "/app/manga.db",
        "/app/cache.sqlite3",
        "/app/__pycache__/main.pyc",
        "/app/.env.production",
        "/app/operator.key",
        "/app/.mangarr-secret-key",
        "/app/test_confirm_flow.py",
        "/app/verify_e2e.py",
    ],
)
def test_release_image_forbidden_file_policy(path):
    assert forbidden_app_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/app/main.py",
        "/app/VERSION",
        "/app/static/alpine.min.js",
        "/app/templates/index.html",
    ],
)
def test_release_image_allows_tracked_runtime_files(path):
    assert not forbidden_app_path(path)


def test_dockerfile_has_release_identity_labels():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    first_line = dockerfile.splitlines()[0]
    assert re.fullmatch(r"FROM python:3\.14-slim@sha256:[0-9a-f]{64}", first_line)
    for marker in (
        "ARG MANGARR_VERSION=dev",
        "COPY LICENSE /app/LICENSE",
        'org.opencontainers.image.licenses="AGPL-3.0-only"',
        "org.opencontainers.image.version",
        "org.opencontainers.image.revision",
        "org.opencontainers.image.created",
    ):
        assert marker in dockerfile
    assert dockerfile.index("RUN pip install") < dockerfile.index("ARG BUILD_DATE")


def test_release_version_invalidates_os_package_layer() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = [
        line.strip()
        for line in dockerfile.replace("\\\n", " ").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    apt_runs = [
        index
        for index, instruction in enumerate(instructions)
        if instruction.startswith("RUN ") and "apt-get update" in instruction
    ]
    assert len(apt_runs) == 1
    apt_index = apt_runs[0]
    apt_run = instructions[apt_index]
    assert "apt-get upgrade -y" in apt_run
    assert apt_run.index("apt-get update") < apt_run.index("apt-get upgrade")
    stage_start = max(
        index
        for index, instruction in enumerate(instructions[:apt_index])
        if instruction.startswith("FROM ")
    )
    assert instructions.count("ARG MANGARR_VERSION=dev") == 1
    assert "ARG MANGARR_VERSION=dev" in instructions[stage_start + 1 : apt_index], (
        "Declare MANGARR_VERSION inside the apt stage before RUN apt-get update "
        "so a new release version invalidates cached OS upgrades"
    )


@pytest.mark.parametrize("argument", ["VCS_REF=unknown", "BUILD_DATE=unknown"])
def test_volatile_release_arguments_stay_after_dependency_and_source_layers(
    argument: str,
) -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = [line.strip() for line in dockerfile.splitlines()]
    assert instructions.count(f"ARG {argument}") == 1
    argument_index = instructions.index(f"ARG {argument}")
    assert all(
        index < argument_index
        for index, instruction in enumerate(instructions)
        if instruction.startswith(("RUN ", "COPY "))
    )


@pytest.mark.parametrize(
    ("path", "expected_builds", "version_argument"),
    [
        ("Makefile", 2, '--build-arg MANGARR_VERSION="$(VERSION)"'),
        (
            ".github/workflows/release.yml",
            1,
            '--build-arg MANGARR_VERSION="${{ steps.release.outputs.version }}"',
        ),
    ],
)
def test_release_shell_builds_supply_version_cache_key(
    path: str, expected_builds: int, version_argument: str
) -> None:
    source = (REPO_ROOT / path).read_text(encoding="utf-8").replace("\\\n", " ")
    builds = re.findall(r"(?m)^\s*docker (?:build|buildx build)\s+([^\n]+)", source)
    assert len(builds) == expected_builds
    for build in builds:
        assert version_argument in build, f"{path}: release build lacks version"


def test_release_publish_action_supplies_version_cache_key() -> None:
    workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert workflow.count("uses: docker/build-push-action@") == 1
    publish_step = workflow.split("uses: docker/build-push-action@", 1)[1].split(
        "\n      - ", 1
    )[0]
    assert re.search(
        r"build-args: \|\n(?:[ \t]+[^\n]*\n)*?"
        r"[ \t]+MANGARR_VERSION=\$\{\{ steps\.release\.outputs\.version \}\}\n",
        publish_step,
    )


def test_local_publish_requires_clean_tagged_commit_and_refuses_replacement():
    makefile = (REPO_ROOT / "Makefile").read_text()
    for marker in (
        "git status --porcelain",
        'git tag --points-at HEAD | grep -Fxq "v$(VERSION)"',
        "Refusing to replace published image tag",
    ):
        assert marker in makefile
    assert "scripts/verify_release_image.py" in makefile


def test_security_config_scan_skips_generated_test_caches():
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "--skip-dirs .ruff_cache" in makefile
    assert "--skip-dirs .pytest_cache" in makefile


def test_local_test_harness_does_not_require_dev_scripts_in_release_image():
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "cd app && $(PYTHON) test_confirm_flow.py" in makefile
    assert (
        "docker exec -i mangarr $(PYTHON) - /config/manga_arr.db "
        "< app/verify_e2e.py"
    ) in makefile
    assert "docker exec mangarr $(PYTHON) /app/test_confirm_flow.py" not in makefile
    assert "docker exec mangarr $(PYTHON) /app/verify_e2e.py" not in makefile
