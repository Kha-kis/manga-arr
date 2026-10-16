"""Use the repository's pinned public Python base for the stdlib-only actor."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path


def python_base_reference(dockerfile: Path) -> str:
    instructions: list[list[str]] = []
    message = "Python actor requires one literal digest-pinned official Python FROM"
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.split(maxsplit=1)[0].upper() != "FROM":
            continue
        try:
            instructions.append(shlex.split(line, comments=True, posix=False))
        except ValueError as exc:
            raise ValueError(message) from exc
    if len(instructions) != 1 or len(instructions[0]) != 2:
        raise ValueError(message)
    image = instructions[0][1]
    if (
        re.fullmatch(
            r"(?:docker\.io/library/)?python:[A-Za-z0-9][A-Za-z0-9_.-]*@sha256:[0-9a-f]{64}",
            image,
        )
        is None
    ):
        raise ValueError(message)
    return image


def _docker(arguments: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Python actor image preflight failed: {exc}") from exc


def ensure_python_base_image(dockerfile: Path) -> str:
    image = python_base_reference(dockerfile)
    inspected = _docker(["image", "inspect", image], timeout=10)
    if inspected.returncode == 0:
        return image
    if "No such image:" not in inspected.stderr:
        raise RuntimeError(
            f"Python actor image inspection failed: {inspected.stderr.strip()}"
        )
    pulled = _docker(["pull", image], timeout=120)
    if pulled.returncode != 0:
        raise RuntimeError(f"Python actor image pull failed: {pulled.stderr.strip()}")
    inspected = _docker(["image", "inspect", image], timeout=10)
    if inspected.returncode != 0:
        raise RuntimeError(
            f"Python actor image unavailable after pull: {inspected.stderr.strip()}"
        )
    return image
