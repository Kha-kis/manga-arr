"""Apply container process settings before starting Mangarr commands."""

from __future__ import annotations

import os
import re
import sys


_UMASK_PATTERN = re.compile(r"^[0-7]{3,4}$")


def parse_umask(value: str) -> int:
    normalized = str(value or "").strip()
    if not _UMASK_PATTERN.fullmatch(normalized):
        raise ValueError("MANGARR_UMASK must contain three or four octal digits")
    parsed = int(normalized, 8)
    if parsed > 0o777:
        raise ValueError("MANGARR_UMASK cannot be more restrictive than 0777")
    return parsed


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write("Mangarr container entrypoint requires a command\n")
        return 64
    try:
        os.umask(parse_umask(os.environ.get("MANGARR_UMASK", "0022")))
    except ValueError as exc:
        sys.stderr.write(f"Mangarr container configuration error: {exc}\n")
        return 64
    os.execvp(args[0], args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
