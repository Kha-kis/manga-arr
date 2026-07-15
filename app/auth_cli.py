"""Offline recovery commands for local administrator access."""

from __future__ import annotations

import argparse
import sys

from auth import reset_admin_for_recovery


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auth_cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    reset = subparsers.add_parser(
        "reset-admin",
        help="remove the administrator and revoke every browser session",
    )
    reset.add_argument(
        "--yes",
        action="store_true",
        help="confirm the administrator reset",
    )
    args = parser.parse_args(argv)

    if args.command == "reset-admin":
        if not args.yes:
            parser.error("reset-admin requires --yes")
        token_path = reset_admin_for_recovery()
        sys.stdout.write(
            "Administrator reset and browser sessions revoked.\n"
            f"Complete setup using the one-time token at {token_path}.\n"
        )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
