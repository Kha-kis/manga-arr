#!/usr/bin/env python3
"""Verify release image identity and reject local artifacts in /app."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import PurePosixPath


FORBIDDEN_SUFFIXES = (
    ".db",
    ".key",
    ".log",
    ".pem",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
)


def forbidden_app_path(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name.startswith(".mangarr-")
        or name.startswith("test_")
        or name == "verify_e2e.py"
        or name.endswith(FORBIDDEN_SUFFIXES)
        or ".db-" in name
        or ".sqlite-" in name
    )


def _run(*args: str) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def verify_image(image: str, version: str, revision: str) -> None:
    details = json.loads(_run("docker", "image", "inspect", image))[0]
    config = details.get("Config") or {}
    labels = config.get("Labels") or {}

    expected_labels = {
        "org.opencontainers.image.title": "Mangarr",
        "org.opencontainers.image.source": "https://github.com/Kha-kis/manga-arr",
        "org.opencontainers.image.licenses": "AGPL-3.0-only",
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": revision,
    }
    for key, expected in expected_labels.items():
        if labels.get(key) != expected:
            raise ValueError(
                f"image label {key!r} is {labels.get(key)!r}, expected {expected!r}"
            )

    created = labels.get("org.opencontainers.image.created", "")
    try:
        datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid OCI build timestamp: {created!r}") from exc

    if config.get("User") in (None, "", "0", "root"):
        raise ValueError("release image must declare a non-root runtime user")

    embedded_version = _run(
        "docker", "run", "--rm", "--entrypoint", "cat", image, "/app/VERSION"
    )
    if embedded_version != version:
        raise ValueError(
            f"embedded version {embedded_version!r} does not match {version!r}"
        )

    inventory = _run(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "find",
        image,
        "/app",
        "-type",
        "f",
        "-print",
    ).splitlines()
    forbidden = [path for path in inventory if forbidden_app_path(path)]
    if forbidden:
        raise ValueError(f"forbidden local artifacts in release image: {forbidden!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    verify_image(args.image, args.version, args.revision)
    print(f"verified {args.image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
