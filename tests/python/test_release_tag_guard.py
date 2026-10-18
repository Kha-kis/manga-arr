"""Offline execution of both real release guards; Docker is always a stub."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION = "2.3.4-rc.1"
IMAGE = "ghcr.io/kha-kis/manga-arr"
REFERENCE = f"{IMAGE}:{VERSION}"
NOT_FOUND = f"ERROR: {REFERENCE}: not found\n"
FAILURES = [
    pytest.param(0, "manifest", "", id="existing"),
    pytest.param(0, "", NOT_FOUND, id="success-with-not-found"),
    pytest.param(1, "", "ERROR: 401 Unauthorized\n", id="401"),
    pytest.param(1, "", "ERROR: 403 Forbidden\n", id="403"),
    pytest.param(1, "", "ERROR: lookup ghcr.io: no such host\n", id="dns"),
    pytest.param(
        1, "", "ERROR: x509: certificate signed by unknown authority\n", id="tls"
    ),
    pytest.param(1, "", "ERROR: connection reset by peer\n", id="network"),
    pytest.param(1, "", "ERROR: 429 Too Many Requests\n", id="429"),
    pytest.param(1, "", "ERROR: 500 Internal Server Error\n", id="500"),
    pytest.param(1, "", "ERROR: 503 Service Unavailable\n", id="503"),
    pytest.param(42, "", "unexpected failure\n", id="unknown42"),
    pytest.param(42, "", NOT_FOUND, id="wrong-status"),
    pytest.param(1, "", "", id="empty"),
    pytest.param(-15, "", "", id="signal"),
    pytest.param(1, "", NOT_FOUND + "ERROR: 401 Unauthorized\n", id="mixed"),
    pytest.param(1, "", NOT_FOUND * 2, id="duplicate"),
    pytest.param(1, "", NOT_FOUND.replace(VERSION, "2.3.4"), id="different-ref"),
    pytest.param(1, "manifest", NOT_FOUND, id="stdout-conflict"),
    pytest.param(1, "\n", NOT_FOUND, id="stdout-whitespace"),
    pytest.param(1, "", "ERROR: manifest not found\n", id="generic-not-found"),
    pytest.param(1, "", NOT_FOUND.rstrip("\n"), id="missing-newline"),
    pytest.param(1, "", NOT_FOUND.replace("\n", "\r\n"), id="unknown-format"),
    pytest.param(1, "", "ERROR: context deadline exceeded\n", id="docker-timeout"),
]


def workflow_guard() -> str:
    workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text()
    guard_heading = "      - name: Refuse to replace an existing release tag\n"
    publish_heading = "      - name: Build and publish multi-architecture image\n"
    assert workflow.index(guard_heading) < workflow.index(publish_heading)
    block = workflow.split(guard_heading, 1)[1].split("\n      - name:", 1)[0]
    return textwrap.dedent(block.split("        run: |\n", 1)[1]).replace(
        "${{ steps.release.outputs.version }}", VERSION
    )


@pytest.fixture
def offline_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(sys.executable)
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """\
            import json
            import os
            from pathlib import Path
            import signal
            import sys

            args = sys.argv[1:]
            with Path(os.environ["STUB_CALLS"]).open("a") as stream:
                stream.write(json.dumps(args) + "\\n")
            if args[:3] == ["buildx", "imagetools", "inspect"]:
                assert args == ["buildx", "imagetools", "inspect", os.environ["STUB_REF"]]
                status, stdout, stderr = json.loads(os.environ["STUB_RESULT"])
                sys.stdout.write(stdout)
                sys.stderr.write(stderr)
                if status < 0:
                    os.kill(os.getpid(), signal.SIGTERM)
                sys.exit(status)
            if args[:2] == ["buildx", "build"] and "--push" in args:
                Path(os.environ["STUB_PUBLISH"]).touch()
                sys.exit(0)
            sys.exit(99)
            """
        )
    )
    docker.chmod(0o755)
    git = bin_dir / "git"
    git.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  "status --porcelain") exit 0 ;;\n'
        f'  "tag --points-at HEAD") printf "%s\\n" "v{VERSION}" ;;\n'
        "  *) exit 99 ;;\n"
        "esac\n"
    )
    git.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "IMAGE": IMAGE,
        "STUB_REF": REFERENCE,
        "STUB_CALLS": str(tmp_path / "calls.jsonl"),
        "STUB_PUBLISH": str(tmp_path / "published"),
    }


def run_release_path(
    path: str, env: dict[str, str], status: int, stdout: str, stderr: str
) -> subprocess.CompletedProcess[str]:
    env = {**env, "STUB_RESULT": json.dumps([status, stdout, stderr])}
    if path == "workflow":
        # The next Action is represented by a stub-only publish sentinel.
        command = [
            "bash",
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            "-c",
            workflow_guard() + "\ndocker buildx build --push .\n",
        ]
    else:
        # Skip the full prerequisite gate, never its actual release-push recipe.
        command = [
            "make",
            "--no-print-directory",
            "--old-file=release-local",
            "release-push",
            f"VERSION={VERSION}",
            f"CONFIRM_RELEASE={VERSION}",
            f"PYTHON={sys.executable}",
            "GIT_SHA=offline-fixture",
            "BUILD_DATE=2026-10-17T00:00:00Z",
            f"RELEASE_TAG_ARGS=--tag {REFERENCE}",
        ]
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


@pytest.mark.parametrize("path", ["workflow", "make"])
@pytest.mark.parametrize(("status", "stdout", "stderr"), FAILURES)
def test_lookup_failure_never_reaches_publish(
    path: str, offline_env: dict[str, str], status: int, stdout: str, stderr: str
) -> None:
    result = run_release_path(path, offline_env, status, stdout, stderr)
    assert not Path(offline_env["STUB_PUBLISH"]).exists(), result
    assert result.returncode != 0, result
    calls = [
        json.loads(line)
        for line in Path(offline_env["STUB_CALLS"]).read_text().splitlines()
    ]
    assert calls == [["buildx", "imagetools", "inspect", REFERENCE]]


@pytest.mark.parametrize("path", ["workflow", "make"])
def test_canonical_absence_can_reach_stub_publish(
    path: str, offline_env: dict[str, str]
) -> None:
    result = run_release_path(path, offline_env, 1, "", NOT_FOUND)
    assert result.returncode == 0, result
    assert Path(offline_env["STUB_PUBLISH"]).exists()
    calls = [
        json.loads(line)
        for line in Path(offline_env["STUB_CALLS"]).read_text().splitlines()
    ]
    assert calls[0] == ["buildx", "imagetools", "inspect", REFERENCE]
    assert len(calls) == 2
    if path == "make":
        tags = [
            calls[1][index + 1] for index, arg in enumerate(calls[1]) if arg == "--tag"
        ]
        assert tags == [REFERENCE]


@pytest.mark.parametrize(
    "error",
    [subprocess.TimeoutExpired("docker", 60), FileNotFoundError(), PermissionError()],
    ids=["timeout", "missing-docker", "os-error"],
)
def test_execution_errors_refuse_without_exposing_diagnostics(error: Exception) -> None:
    from scripts.release_tag_guard import require_absent

    with patch("scripts.release_tag_guard.subprocess.run", side_effect=error):
        with pytest.raises(ValueError, match="Refusing to publish: release tag lookup"):
            require_absent(REFERENCE)


@pytest.mark.parametrize(
    "reference",
    ["", "--help", "repo", "https://user:secret@ghcr.io/repo:1", "repo:1\n"],
)
def test_invalid_reference_never_invokes_docker(reference: str) -> None:
    from scripts.release_tag_guard import require_absent

    with patch("scripts.release_tag_guard.subprocess.run") as run:
        with pytest.raises(ValueError, match="invalid release image reference"):
            require_absent(reference)
        run.assert_not_called()


def test_helper_uses_bounded_authenticated_docker_inspect_only() -> None:
    from scripts.release_tag_guard import require_absent

    result = subprocess.CompletedProcess([], 1, b"", NOT_FOUND.encode("ascii"))
    with patch("scripts.release_tag_guard.subprocess.run", return_value=result) as run:
        require_absent(REFERENCE)
    run.assert_called_once_with(
        ["docker", "buildx", "imagetools", "inspect", REFERENCE],
        capture_output=True,
        check=False,
        timeout=60,
    )


def test_cli_does_not_echo_untrusted_docker_output(offline_env: dict[str, str]) -> None:
    result = run_release_path(
        "workflow", offline_env, 1, "secret-stdout", "secret-stderr"
    )
    assert result.returncode != 0
    assert "secret-stdout" not in result.stdout + result.stderr
    assert "secret-stderr" not in result.stdout + result.stderr
