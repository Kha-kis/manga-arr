#!/usr/bin/env python3
"""Collect or verify maintainer-declared identities of installed crypto runtimes."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import ssl
import sys
from typing import cast


DEFAULT_RECORD = Path("/usr/share/doc/mangarr/crypto-runtime.cdx.json")


@dataclass(frozen=True)
class Observation:
    package_version: str
    embedded_openssl: str
    stdlib_openssl: str
    machine: str
    native_module: Path


def observe() -> Observation:
    """Read live identities; never infer the backend version from a package pin."""
    from cryptography.hazmat.backends.openssl.backend import backend
    from cryptography.hazmat.bindings import _rust

    return Observation(
        package_version=importlib.metadata.version("cryptography"),
        embedded_openssl=backend.openssl_version_text(),
        stdlib_openssl=ssl.OPENSSL_VERSION,
        machine=platform.machine(),
        native_module=Path(_rust.__file__).resolve(strict=True),
    )


def openssl_version(text: str) -> str:
    match = re.match(r"OpenSSL (\d+\.\d+\.\d+)(?: |$)", text)
    if match is None:
        raise ValueError("unsupported OpenSSL version text")
    return match[1]


def collect(observation: Observation | None = None) -> dict[str, object]:
    """Declare observed identities, with the complete carrier hash as a property."""
    observed = observe() if observation is None else observation
    document, _ = _collect(observed)
    return document


def _collect(observed: Observation) -> tuple[dict[str, object], str]:
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(observed.machine)
    if architecture is None:
        raise ValueError("unsupported runtime machine")
    embedded = openssl_version(observed.embedded_openssl)
    stdlib = openssl_version(observed.stdlib_openssl)
    if observed.package_version != "50.0.2" or embedded != "4.0.3":
        raise ValueError("unexpected cryptography or embedded OpenSSL version")
    with observed.native_module.open("rb") as carrier:
        digest = hashlib.file_digest(carrier, "sha256").hexdigest()
    parent_ref = "cryptography"
    embedded_ref = "cryptography-embedded-openssl"
    stdlib_ref = "python-stdlib-openssl"
    document: dict[str, object] = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "properties": [
                {"name": "mangarr:machine", "value": observed.machine},
                {"name": "mangarr:architecture", "value": architecture},
            ],
        },
        "components": [
            {
                "type": "library",
                "bom-ref": parent_ref,
                "name": "cryptography",
                "version": observed.package_version,
                "purl": f"pkg:pypi/cryptography@{observed.package_version}",
            },
            {
                "type": "library",
                "bom-ref": embedded_ref,
                "name": "OpenSSL",
                "version": embedded,
                "purl": f"pkg:generic/openssl@{embedded}",
                "cpe": f"cpe:2.3:a:openssl:openssl:{embedded}:*:*:*:*:*:*:*",
                "description": (
                    "Maintainer-declared embedded runtime identity; not "
                    "auto-discovered or a vulnerability clearance. The native "
                    "module SHA256 property covers the complete carrier, "
                    "not isolated OpenSSL bytes."
                ),
                "properties": [
                    {"name": "mangarr:scope", "value": "cryptography-embedded"},
                    {
                        "name": "mangarr:openssl:version-text",
                        "value": observed.embedded_openssl,
                    },
                    {
                        "name": "mangarr:native-module:path",
                        "value": str(observed.native_module),
                    },
                    {"name": "mangarr:native-module:sha256", "value": digest},
                ],
            },
            {
                "type": "library",
                "bom-ref": stdlib_ref,
                "name": "OpenSSL",
                "version": stdlib,
                "description": (
                    "Maintainer-declared Python ssl runtime identity; "
                    "distinct from cryptography's embedded OpenSSL."
                ),
                "properties": [
                    {"name": "mangarr:scope", "value": "python-stdlib-ssl"},
                    {
                        "name": "mangarr:openssl:version-text",
                        "value": observed.stdlib_openssl,
                    },
                ],
            },
        ],
        "dependencies": [
            {"ref": parent_ref, "dependsOn": [embedded_ref]},
            {"ref": embedded_ref, "dependsOn": []},
            {"ref": stdlib_ref, "dependsOn": []},
        ],
    }
    return document, digest


def encode(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def verify(record: Path, observation: Observation | None = None) -> dict[str, str]:
    """Reobserve installed versions and bytes, ignoring all paths in the record."""
    observed = observe() if observation is None else observation
    expected, digest = _collect(observed)
    stored = cast(
        object,
        json.loads(
            record.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        ),
    )
    if encode(stored) != encode(expected):
        raise ValueError("crypto runtime record mismatch")
    return {
        "cryptography": observed.package_version,
        "embedded_openssl": openssl_version(observed.embedded_openssl),
        "stdlib_openssl": openssl_version(observed.stdlib_openssl),
        "machine": observed.machine,
        "native_module_sha256": digest,
    }


class Arguments(argparse.Namespace):
    action: str = ""
    record: Path = DEFAULT_RECORD


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("collect", "verify"))
    _ = parser.add_argument("--record", type=Path, default=DEFAULT_RECORD)
    args = parser.parse_args(argv, namespace=Arguments())
    try:
        if args.action == "collect":
            document = collect()
            with args.record.open("x", encoding="utf-8") as output:
                _ = output.write(encode(document))
        else:
            print(encode(verify(args.record)), end="")
    except (ValueError, OSError, ImportError, importlib.metadata.PackageNotFoundError):
        print("Crypto runtime identity check failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
