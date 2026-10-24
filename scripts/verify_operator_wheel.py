"""Verify the single operator wheel without extracting or executing its code."""

from __future__ import annotations

import configparser
from email.parser import Parser
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import tomllib
from typing import cast
import zipfile


def _table(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) for k in cast(dict[object, object], value)
    ):
        raise ValueError("expected metadata table")
    return cast(dict[str, object], value)


def verify_wheel(directory: Path, project: Path) -> dict[str, object]:
    """Return the exact artifact digest and module-byte inventory, or refuse."""
    metadata = _table(tomllib.loads(project.read_text(encoding="utf-8")))
    configured = _table(_table(metadata["tool"])["setuptools"])["py-modules"]
    if not isinstance(configured, list):
        raise ValueError("explicit module list required")
    values = cast(list[object], configured)
    if not values or any(
        not isinstance(v, str) or not v.isidentifier() for v in values
    ):
        raise ValueError("invalid module list")
    modules = cast(list[str], values)
    if len(set(modules)) != len(modules):
        raise ValueError("duplicate module")
    if _table(_table(metadata["project"])["scripts"]) != {"mangarr": "cli:main"}:
        raise ValueError("unexpected console entry point")
    wheels = list(directory.glob("*.whl"))
    if len(wheels) != 1 or not wheels[0].is_file() or wheels[0].is_symlink():
        raise ValueError("exactly one regular wheel required")
    wheel = wheels[0].absolute()
    data = wheel.read_bytes()
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("duplicate ZIP member")
        expected = {m + ".py" for m in modules}
        if {n for n in names if n.endswith(".py")} != expected:
            raise ValueError("operator module inventory differs")
        infos = {n.split("/")[0] for n in names if ".dist-info/" in n}
        if len(infos) != 1:
            raise ValueError("one distribution required")
        info = infos.pop()
        message = Parser().parsestr(archive.read(info + "/METADATA").decode("utf-8"))
        version = message.get("Version")
        if message.get_all("Name") != ["mangarr"] or not version:
            raise ValueError("unexpected distribution")
        if (
            message.get_all("Version") != [version]
            or info != f"mangarr-{version}.dist-info"
        ):
            raise ValueError("distribution version differs")
        if not wheel.name.startswith(f"mangarr-{version}-"):
            raise ValueError("wheel filename differs")
        entries = configparser.ConfigParser(interpolation=None)
        entries.optionxform = lambda optionstr: optionstr
        entries.read_string(archive.read(info + "/entry_points.txt").decode("utf-8"))
        if entries.sections() != ["console_scripts"] or entries.defaults():
            raise ValueError("unexpected entry point groups")
        if dict(entries.items("console_scripts")) != {"mangarr": "cli:main"}:
            raise ValueError("unregistered operator launcher")
        inventory = {
            m: hashlib.sha256(archive.read(m + ".py")).hexdigest()
            for m in sorted(modules)
        }
    return {
        "wheel": str(wheel),
        "sha256": hashlib.sha256(data).hexdigest(),
        "name": "mangarr",
        "version": version,
        "modules": inventory,
    }


def main(arguments: list[str] | None = None) -> int:
    """Validate one wheel directory and emit one JSON qualification receipt."""
    args = sys.argv[1:] if arguments is None else arguments
    if len(args) != 2:
        _ = sys.stderr.write("operator_wheel_arguments\n")
        return 2
    try:
        report = verify_wheel(Path(args[0]), Path(args[1]))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, configparser.Error):
        _ = sys.stderr.write("operator_wheel_invalid\n")
        return 1
    _ = sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
