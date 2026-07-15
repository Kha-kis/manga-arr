#!/usr/bin/env python3
"""Validate release identity and generate immutable container tags."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "app/VERSION"
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
IMAGE_RE = re.compile(r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")


def read_version() -> str:
    return VERSION_FILE.read_text(encoding="ascii").strip()


def parse_version(version: str) -> tuple[int, int, int, str | None]:
    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise ValueError(f"app/VERSION is not supported SemVer: {version!r}")
    prerelease = match.group(4)
    if prerelease:
        for identifier in prerelease.split("."):
            if identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0":
                raise ValueError(
                    f"numeric prerelease identifier has a leading zero: {identifier!r}"
                )
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease


def image_tags(version: str, image: str) -> list[str]:
    if not IMAGE_RE.fullmatch(image):
        raise ValueError(f"invalid container image name: {image!r}")
    major, minor, _patch, prerelease = parse_version(version)
    tags = [f"{image}:{version}"]
    if not prerelease:
        tags.extend(
            [
                f"{image}:{major}.{minor}",
                f"{image}:{major}",
                f"{image}:latest",
            ]
        )
    return tags


def validate_release(version: str, tag: str | None = None) -> None:
    parse_version(version)
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Git tag {tag!r} must exactly match v{version}")

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if version not in readme:
        raise ValueError(f"README.md does not name current version {version}")
    if f"## {version} " not in changelog:
        raise ValueError(f"CHANGELOG.md has no release heading for {version}")


def _write_github_output(path: Path, metadata: dict[str, object]) -> None:
    tags = metadata["tags"]
    with path.open("a", encoding="utf-8") as output:
        output.write(f"version={metadata['version']}\n")
        output.write(f"prerelease={str(metadata['prerelease']).lower()}\n")
        output.write(f"build_date={metadata['build_date']}\n")
        output.write("tags<<MANGARR_TAGS\n")
        output.write("\n".join(str(tag) for tag in tags))
        output.write("\nMANGARR_TAGS\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="Git tag to require, including the v prefix")
    parser.add_argument("--image", default="ghcr.io/kha-kis/manga-arr")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument(
        "--format",
        choices=("json", "docker-args"),
        default="json",
    )
    args = parser.parse_args()

    version = read_version()
    validate_release(version, args.tag)
    _major, _minor, _patch, prerelease = parse_version(version)
    tags = image_tags(version, args.image)
    metadata: dict[str, object] = {
        "version": version,
        "prerelease": prerelease is not None,
        "build_date": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "tags": tags,
    }

    if args.github_output:
        _write_github_output(args.github_output, metadata)
    if args.format == "docker-args":
        print(" ".join(f"--tag {tag}" for tag in tags))
    else:
        print(json.dumps(metadata, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
