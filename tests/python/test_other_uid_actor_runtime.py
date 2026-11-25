"""Portable runtime setup must not weaken the real other-UID attack."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from tests.python.other_uid_actor import python_base_reference


PIN = "python:fixture-slim@sha256:" + "a" * 64


@pytest.mark.parametrize("alias", ["", " AS runtime"])
def test_multistage_selects_actual_final_literal_python(
    tmp_path: Path, alias: str
) -> None:
    final = "python:final-slim@sha256:" + "b" * 64
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM " + PIN + " AS builder\nRUN true\nFROM " + final + alias + "\n"
    )
    assert python_base_reference(dockerfile) == final


def test_continued_run_cannot_manufacture_a_from_instruction(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM "
        + PIN
        + "\nRUN printf '%s' "
        + chr(92)
        + "\nFROM attacker/python:latest\n"
    )
    assert python_base_reference(dockerfile) == PIN


@pytest.mark.parametrize(
    "final",
    [
        "FROM builder",
        "FROM ${BASE}",
        "FROM python:untrusted-slim",
        "FROM private/python:fixture-slim@sha256:" + "a" * 64,
        "FROM " + PIN + " AS ${NAME}",
        "FROM --platform=linux/amd64 " + PIN,
        "FROM " + PIN + " AS runtime extra",
        "FROM " + PIN + " " + chr(92) + "\nAS runtime",
    ],
)
def test_multistage_refuses_nonliteral_or_ambiguous_final(
    tmp_path: Path, final: str
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM " + PIN + " AS builder\nARG BASE\n" + final + "\n")
    with pytest.raises(ValueError, match="pinned official Python"):
        python_base_reference(dockerfile)


@pytest.mark.parametrize(
    "content",
    [
        "# escape=`\nFROM " + PIN + "\n",
        "FROM " + PIN + "\nRUN <<EOF\nFROM attacker\nEOF\n",
        "FROM " + PIN + " AS same\nFROM " + PIN + " AS same\n",
    ],
)
def test_refuses_unparsed_dockerfile_constructs(tmp_path: Path, content: str) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(content)
    with pytest.raises(ValueError, match="pinned official Python"):
        python_base_reference(dockerfile)


@pytest.mark.parametrize("prefix", ["python", "docker.io/library/python"])
def test_selects_literal_pinned_official_python_base(
    tmp_path: Path, prefix: str
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "# actor follows the product base\nFROM "
        + prefix
        + ":fixture-slim@sha256:"
        + "a" * 64
        + "\nRUN true\n"
    )
    assert (
        python_base_reference(dockerfile) == prefix + ":fixture-slim@sha256:" + "a" * 64
    )


@pytest.mark.parametrize(
    "content",
    [
        "FROM python:fixture-slim\n",
        "FROM private/python:fixture-slim@sha256:" + "a" * 64 + "\n",
        "ARG BASE\nFROM ${BASE}\n",
        "FROM python:fixture-slim@sha256:abc\n",
        "FROM " + PIN + " AS build\nFROM scratch\n",
        "FROM --platform=linux/amd64 " + PIN + "\n",
        "FROM " + PIN + "; echo unsafe\n",
        "FROM " + PIN + " extra\n",
        "# no base\nRUN true\n",
    ],
)
def test_refuses_unpinned_dynamic_or_ambiguous_bases(
    tmp_path: Path, content: str
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(content)
    with pytest.raises(ValueError, match="pinned official Python"):
        python_base_reference(dockerfile)


@pytest.fixture
def dockerfile(tmp_path: Path) -> Path:
    path = tmp_path / "Dockerfile"
    path.write_text("FROM " + PIN + "\n")
    return path


@pytest.mark.parametrize("cached", [True, False])
def test_preflight_reuses_cache_or_pulls_exact_pin_once(
    dockerfile: Path, monkeypatch: pytest.MonkeyPatch, cached: bool
) -> None:
    from tests.python import other_uid_actor as runtime

    commands: list[list[str]] = []
    expected = [["docker", "image", "inspect", PIN]]
    if not cached:
        expected += [["docker", "pull", PIN], ["docker", "image", "inspect", PIN]]

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "check": False,
            "timeout": 120 if command[1] == "pull" else 10,
        }
        absent = not cached and len(commands) == 1
        return subprocess.CompletedProcess(
            command,
            1 if absent else 0,
            "",
            "Error: No such image: " + PIN if absent else "",
        )

    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert runtime.ensure_python_base_image(dockerfile) == PIN
    assert commands == expected


@pytest.mark.parametrize(
    "failure",
    ["daemon", "pull", "recheck", "inspect_timeout", "pull_timeout", "missing_docker"],
)
def test_preflight_fails_closed_without_running_actor(
    dockerfile: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from tests.python import other_uid_actor as runtime

    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if failure == "missing_docker":
            raise FileNotFoundError("docker")
        if (
            failure == "inspect_timeout"
            or failure == "pull_timeout"
            and command[1] == "pull"
        ):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if failure == "daemon":
            return subprocess.CompletedProcess(
                command, 1, "", "Cannot connect to Docker daemon"
            )
        if command[1] == "pull":
            return subprocess.CompletedProcess(
                command, 1 if failure == "pull" else 0, "", "pull refused"
            )
        absent = len(commands) == 1 or failure == "recheck"
        return subprocess.CompletedProcess(
            command, 1 if absent else 0, "", "No such image: " + PIN
        )

    monkeypatch.setattr(runtime.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Python actor"):
        runtime.ensure_python_base_image(dockerfile)
    assert all(command[:2] != ["docker", "run"] for command in commands)
    assert sum(command[1] == "pull" for command in commands) == (
        1 if failure in ("pull", "recheck", "pull_timeout") else 0
    )
