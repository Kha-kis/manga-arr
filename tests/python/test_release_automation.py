"""Public release automation invariants."""

import hashlib
import re
import shlex
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
            or (len(parts) == 3 and parts[1] == "templates" and path.suffix == ".html")
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


def _assert_build_stages(dockerfile: str) -> None:
    stages = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(stages) == 3, "expected exactly three build stages"
    for stage, name in zip(
        stages, ("archive-fetcher", "sqlite-builder", "runtime"), strict=True
    ):
        assert re.fullmatch(
            r"FROM python:3\.14-slim@sha256:[0-9a-f]{64} AS " + name, stage
        )
    assert len({stage.split()[1] for stage in stages}) == 1, (
        "all build stages must use the same pinned base"
    )


@pytest.mark.parametrize("stage_index", [0, 1, 2])
def test_build_stages_refuse_missing_stage(stage_index: int) -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    stages = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    del stages[stage_index]
    with pytest.raises(AssertionError, match="exactly three build stages"):
        _assert_build_stages("\n".join(stages))


@pytest.mark.parametrize("stage_index", [0, 1, 2])
def test_build_stages_refuse_mismatched_pin(stage_index: int) -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    stages = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    prefix, separator, suffix = stages[stage_index].partition("@sha256:")
    assert separator
    stages[stage_index] = prefix + separator + "a" * 64 + suffix[64:]
    with pytest.raises(AssertionError, match="same pinned base"):
        _assert_build_stages("\n".join(stages))


def test_fast_and_format_gates_preserve_both_runtime_build_helpers() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    lint = makefile.split("\nlint:\n", 1)[1].split("\ntest-fast:", 1)[0]
    fast = makefile.split("\ntest-fast: lint\n", 1)[1].split("\ntest-python:", 1)[0]
    for path in (
        "scripts/install_7zip.py",
        "scripts/sqlite_runtime.py",
        "tests/python/test_7zip_runtime.py",
        "tests/python/test_sqlite_runtime.py",
    ):
        assert path in lint
    for path in (
        "tests/python/test_7zip_runtime.py",
        "tests/python/test_sqlite_runtime.py",
        "tests/python/test_other_uid_actor_runtime.py",
    ):
        assert path in fast


def test_dockerfile_has_release_identity_labels():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    _assert_build_stages(dockerfile)
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


def test_dockerfile_removes_build_only_pip_after_dependency_installation() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    run_commands = "\n".join(
        line.strip()
        for line in dockerfile.replace("\\\n", " ").splitlines()
        if line.strip().startswith("RUN ")
    )
    dependency_install = re.search(
        r"\bpip\s+install\s+[^;&\n]*-r\s+/tmp/requirements\.txt\b", run_commands
    )
    assert dependency_install is not None
    pip_removal = re.search(r"\bpip\s+uninstall\s+[^;&\n]*\bpip\b", run_commands)
    assert pip_removal is not None, (
        "Remove build-only pip and its vendored dependencies from the release image"
    )
    assert dependency_install.end() < pip_removal.start(), (
        "Remove build-only pip only after installing runtime dependencies"
    )


def _dockerfile_runs() -> list[str]:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    return [
        line.strip()[4:]
        for line in dockerfile.replace("\\\n", " ").splitlines()
        if line.strip().startswith("RUN ")
    ]


@pytest.mark.parametrize("operation", ["update", "upgrade", "install"])
def test_apt_acquisition_explicitly_disables_retries(operation: str) -> None:
    acquisitions = [
        tokens
        for run in _dockerfile_runs()
        for command in run.split("&&")
        if (tokens := shlex.split(command, comments=True))
        and tokens[0] == "apt-get" and operation in tokens
    ]
    assert acquisitions, f"Expected an apt-get {operation} acquisition"
    for tokens in acquisitions:
        assert ("-o", "Acquire::Retries=0") in list(zip(tokens, tokens[1:])), (
            f"Every apt-get {operation} acquisition must explicitly set -o Acquire::Retries=0"
        )
        assert all(
            token == "Acquire::Retries=0"
            for token in tokens if token.startswith("Acquire::Retries=")
        ), f"apt-get {operation} must not override Acquire::Retries=0"


@pytest.mark.parametrize("package", ["apt", "libapt-pkg7.0"])
def test_apt_fixed_version_is_guarded_before_package_install(package: str) -> None:
    install_runs = 0
    for run in _dockerfile_runs():
        commands = [shlex.split(command, comments=True) for command in run.split("&&")]
        installs = [
            index for index, tokens in enumerate(commands)
            if tokens and tokens[0] == "apt-get" and "install" in tokens
        ]
        if not installs:
            continue
        install_runs += 1
        upgrades = [
            index for index, tokens in enumerate(commands)
            if tokens and tokens[0] == "apt-get" and "upgrade" in tokens
        ]
        guards = [
            index for index, tokens in enumerate(commands)
            if len(tokens) == 5 and tokens[:2] == ["dpkg", "--compare-versions"]
            and tokens[3:] == ["ge", "3.0.4"]
            and tokens[2].startswith("$(") and tokens[2].endswith(")")
            and shlex.split(tokens[2][2:-1]) == [
                "dpkg-query", "-W", "-f=${Version}", package,
            ]
        ]
        assert guards, f"Require an actual dpkg-query/dpkg version guard for {package} >=3.0.4"
        assert upgrades and max(upgrades) < min(guards) <= max(guards) < min(installs), (
            f"Guard {package} after upgrade and before any package installation"
        )
    assert install_runs, "Expected an apt-get install RUN with bootstrap version guards"


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
    assert len(apt_runs) == 2
    assert instructions.count("ARG MANGARR_VERSION=dev") == 2
    for apt_index in apt_runs:
        apt_run = instructions[apt_index]
        assert "apt-get upgrade -y" in apt_run
        assert apt_run.index("apt-get update") < apt_run.index("apt-get upgrade")
        stage_start = max(
            index
            for index, instruction in enumerate(instructions[:apt_index])
            if instruction.startswith("FROM ")
        )
        assert "ARG MANGARR_VERSION=dev" in instructions[stage_start + 1 : apt_index], (
            "Declare MANGARR_VERSION inside each apt stage before RUN apt-get update "
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
        'scripts/release_tag_guard.py "$(RELEASE_IMAGE):$(VERSION)"',
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
        "docker exec -i mangarr $(PYTHON) - /config/manga_arr.db < app/verify_e2e.py"
    ) in makefile
    assert "docker exec mangarr $(PYTHON) /app/test_confirm_flow.py" not in makefile
    assert "docker exec mangarr $(PYTHON) /app/verify_e2e.py" not in makefile


def _native_sqlite_workflow() -> str:
    path = REPO_ROOT / ".github/workflows/native-sqlite-build.yml"
    assert path.is_file(), "Missing manual native ARM64 build evidence workflow"
    return path.read_text(encoding="utf-8")


def test_native_sqlite_build_is_manual_and_least_privilege() -> None:
    workflow = _native_sqlite_workflow()
    triggers = workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"^  ([a-z_]+):", triggers, re.MULTILINE) == ["workflow_dispatch"]
    assert "revision:\n" in triggers
    assert "required: true" in triggers and "type: string" in triggers
    permissions = workflow.split("\npermissions:\n", 1)[1].split("\njobs:", 1)[0]
    assert permissions.strip() == "contents: read"
    assert "if: github.repository == 'Kha-kis/manga-arr'" in workflow
    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" in workflow
    assert "persist-credentials: false" in workflow
    assert "ref: ${{ inputs.revision }}" in workflow
    assert not re.search(
        r"secrets\.|docker\s+(?:login|push|save)|packages:|id-token:|--push|--secret",
        workflow,
    )


def test_native_sqlite_build_checks_native_host_and_exact_revision_first() -> None:
    workflow = _native_sqlite_workflow()
    assert "runs-on: ubuntu-24.04-arm" in workflow
    assert "timeout-minutes: 30" in workflow
    assert "REVISION: ${{ inputs.revision }}" in workflow
    assert "WORKFLOW_REVISION: ${{ github.sha }}" in workflow
    validation = workflow.index('[[ "$REVISION" =~ ^[0-9a-f]{40}$ ]]')
    checkout = workflow.index("uses: actions/checkout@")
    identity = workflow.index('test "$(git rev-parse HEAD)" = "$REVISION"')
    build = workflow.index("docker build ")
    assert validation < checkout < identity < build
    for witness in ('test "$RUNNER_ARCH" = ARM64', 'test "$(uname -m)" = aarch64'):
        assert workflow.index(witness) < build
    assert not re.search(
        r"(?i)qemu|buildx|--platform|DOCKER_DEFAULT_PLATFORM|retries:|retry|continue-on-error",
        workflow,
    )


def test_native_sqlite_build_is_single_normal_build_with_bound_identity() -> None:
    workflow = _native_sqlite_workflow()
    assert len(re.findall(r"\bdocker build\s", workflow)) == 1
    assert "OWNER: ${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert (
        "IMAGE_TAG: mangarr-native-arm64:${{ github.run_id }}-${{ github.run_attempt }}"
        in workflow
    )
    for marker in (
        "set -euo pipefail",
        '--progress=plain --iidfile "$EVIDENCE_DIR/image.id"',
        '--tag "$IMAGE_TAG"',
        '--label "io.mangarr.native-build=$OWNER"',
        '--build-arg "MANGARR_VERSION=dev-native-arm64-$OWNER"',
        '--build-arg "VCS_REF=$REVISION"',
        '--label "io.mangarr.workflow-revision=$WORKFLOW_REVISION"',
        '--build-arg "BUILD_DATE=$build_date"',
        '2>&1 | tee "$EVIDENCE_DIR/build.log"',
        '.os == "linux"',
        '.architecture == "arm64"',
        '.user == "mangarr"',
        ".source_revision == $revision",
        ".id == $image_id",
        "source_revision: $revision",
        "workflow_revision: $workflow_revision",
        ".workflow_revision == $workflow_revision",
    ):
        assert marker in workflow
    commands = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(
        r"(?i)qualify-wal|repro|crash|recovery|\bSELECT\b|\bPRAGMA\b|docker\s+(?:run|start|exec)",
        commands,
    )


def test_native_sqlite_evidence_uses_only_never_started_provenance_container() -> None:
    workflow = _native_sqlite_workflow()
    assert len(re.findall(r"\bdocker create\s", workflow)) == 1
    assert len(re.findall(r"\bdocker cp\s", workflow)) == 1
    for marker in (
        "CONTAINER_NAME: mangarr-native-arm64-${{ github.run_id }}-${{ github.run_attempt }}",
        '--cidfile "$EVIDENCE_DIR/container.id"',
        '--name "$CONTAINER_NAME"',
        "--network none --read-only --cap-drop ALL",
        "--security-opt no-new-privileges",
        '--entrypoint /bin/false "$image_id"',
        '"$container_id:/opt/mangarr/sqlite/3.54.1/provenance.json"',
        '.machine == "aarch64"',
        '.version == "3.54.1"',
        '.source_id | test("^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9a-f]{64}$")',
        'test("^[0-9a-f]{64}$")',
        ".source_sha3_256",
        ".library_sha256",
        ".extension_sha256",
    ):
        assert marker in workflow
    assert not re.search(
        r"--mount|--volume|(?:^|\s)-v\s|--privileged|\.Config.Env|\{\{json \.\}\}",
        workflow,
    )
    for line in workflow.splitlines():
        if re.search(r"docker (?:image|container) inspect", line):
            assert "--format" in line


def test_native_sqlite_cleanup_is_always_owned_exact_and_fail_closed() -> None:
    workflow = _native_sqlite_workflow()
    cleanup = workflow.split("- name: Clean up owned Docker objects\n", 1)[1].split(
        "\n      - name:", 1
    )[0]
    assert "if: always()" in cleanup
    for marker in (
        '[[ "$container_id" =~ ^[0-9a-f]{64}$ ]]',
        '[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]',
        ".id == $container_id",
        ".owner == $owner",
        ".image == $image_id",
        '.status == "created"',
        ".running == false",
        ".id == $image_id",
        'docker container rm "$container_id"',
        'docker image rm "$image_id"',
        'exit "$status"',
    ):
        assert marker in cleanup
    assert "|| return 1" in cleanup
    assert not re.search(
        r"--force|(?:^|\s)-f(?:\s|$)|\bprune\b|\|\| true|rm \"\$IMAGE_TAG\"", cleanup
    )


def test_native_sqlite_artifacts_are_fixed_compact_and_short_lived() -> None:
    workflow = _native_sqlite_workflow()
    assert (
        "EVIDENCE_DIR: ${{ runner.temp }}/mangarr-native-arm64-${{ github.run_id }}-${{ github.run_attempt }}"
        in workflow
    )
    upload = workflow.split("- name: Upload build evidence\n", 1)[1]
    assert "if: always()" in upload
    assert "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9" in upload
    assert "retention-days: 7" in upload
    assert "if-no-files-found: error" in upload
    paths = re.findall(r"^            (\S.*)$", upload, re.MULTILINE)
    assert paths == [
        "${{ runner.temp }}/mangarr-native-arm64-${{ github.run_id }}-${{ github.run_attempt }}/*.json",
        "${{ runner.temp }}/mangarr-native-arm64-${{ github.run_id }}-${{ github.run_attempt }}/*.log",
        "${{ runner.temp }}/mangarr-native-arm64-${{ github.run_id }}-${{ github.run_attempt }}/*.id",
    ]


def test_native_sqlite_runner_context_is_scoped_to_steps() -> None:
    workflow = _native_sqlite_workflow()
    job_scope, steps = workflow.split("\n    steps:\n", 1)
    assert not re.search(r"\$\{\{\s*runner\b", job_scope), (
        "The runner context is unavailable in job-level env and other job scope"
    )
    evidence_path = "${{ runner.temp }}/mangarr-native-arm64-${{ github.run_id }}-${{ github.run_attempt }}"
    run_steps = [
        step for step in steps.split("      - name: ") if "\n        run: |\n" in step
    ]
    assert len(run_steps) == 4
    for step in run_steps:
        step_scope = step.split("\n        run: |\n", 1)[0]
        assert (
            f"\n        env:\n          EVIDENCE_DIR: {evidence_path}\n"
            in step_scope + "\n"
        )
    upload = steps.split("- name: Upload build evidence\n", 1)[1]
    assert "${{ env.EVIDENCE_DIR }}" not in upload
    assert "GITHUB_ENV" not in workflow
