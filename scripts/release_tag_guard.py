#!/usr/bin/env python3
"""Allow publication only after a canonical Buildx exact-tag absence result."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys


REFERENCE_RE = re.compile(
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}",
    re.ASCII,
)


def require_absent(reference: str) -> None:
    """Refuse existing tags and any lookup that does not prove exact absence."""
    if not REFERENCE_RE.fullmatch(reference):
        raise ValueError("invalid release image reference")
    try:
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", reference],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            "Refusing to publish: release tag lookup could not complete"
        ) from exc

    if result.returncode == 0:
        raise ValueError(f"Refusing to replace published image tag: {reference}")
    # Compare raw bytes: whitespace, additional diagnostics, or another reference
    # must not turn an indeterminate lookup into permission to publish.
    if (
        result.returncode != 1
        or result.stdout != b""
        or result.stderr != f"ERROR: {reference}: not found\n".encode("ascii")
    ):
        raise ValueError(
            "Refusing to publish: release tag lookup did not prove absence "
            f"(Docker exit status {result.returncode})"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", help="Full exact-version image reference")
    args = parser.parse_args()
    try:
        require_absent(args.reference)
    except ValueError as exc:
        # Do not echo Docker output, which can contain authentication details.
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
