#!/usr/bin/env python3
"""Stage authenticated upstream 7-Zip files for a container COPY, never a host install."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
from http.client import HTTPMessage, HTTPResponse
import io
import json
import lzma
from pathlib import Path
import shutil
import struct
import sys
import tarfile
import time
from typing import IO, cast
import urllib.error
import urllib.parse
import urllib.request


VERSION = "26.06"
SOURCE_URL = f"https://github.com/ip7z/7zip/releases/tag/{VERSION}"
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_TAR_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 256


@dataclass(frozen=True)
class ArchivePin:
    filename: str
    sha256: str
    machine: int

    @property
    def url(self) -> str:
        return (
            f"https://github.com/ip7z/7zip/releases/download/{VERSION}/{self.filename}"
        )


PINS = {
    "amd64": ArchivePin(
        "7z2606-linux-x64.tar.xz",
        "2b539cf067b2ad48e1baa5093dbaa8818b73542d1222a0ffde9dc5f2922ea62e",
        62,
    ),
    "arm64": ArchivePin(
        "7z2606-linux-arm64.tar.xz",
        "edcc408aacce77da5223728f2eceb5e9f6dc713b8a9df934ad21e2a7f7560627",
        183,
    ),
}


def pin_for_arch(architecture: str) -> ArchivePin:
    """Accept Docker TARGETARCH names only; never infer the build host's CPU."""
    try:
        return PINS[architecture]
    except KeyError:
        raise ValueError("unsupported archive architecture") from None


def _require_https(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("archive transport requires credential-free HTTPS")


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        _require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_archive(pin: ArchivePin) -> bytes:
    """Bound both network reads and elapsed download time, including redirects."""
    _require_https(pin.url)
    opener = urllib.request.build_opener(HTTPSRedirectHandler())
    deadline = time.monotonic() + 120
    request = urllib.request.Request(
        pin.url, headers={"User-Agent": "Mangarr-archive-build"}
    )
    with cast(HTTPResponse, opener.open(request, timeout=30)) as response:
        _require_https(response.url)
        length = response.headers.get("Content-Length")
        expected = None
        if length is not None:
            if not length.isascii() or not length.isdecimal():
                raise ValueError("invalid archive Content-Length")
            expected = int(length)
            if expected > MAX_ARCHIVE_BYTES:
                raise ValueError("archive download size limit exceeded")
        data = bytearray()
        while True:
            if time.monotonic() > deadline:
                raise ValueError("archive download time limit exceeded")
            chunk = response.read(min(65536, MAX_ARCHIVE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_ARCHIVE_BYTES:
                raise ValueError("archive download size limit exceeded")
        if expected is not None and len(data) != expected:
            raise ValueError("archive download length mismatch")
    return bytes(data)


def unpack_archive(data: bytes, pin: ArchivePin) -> dict[str, bytes]:
    """Authenticate before parsing and read only the three required regular files."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive size limit exceeded")
    if hashlib.sha256(data).hexdigest() != pin.sha256:
        raise ValueError("archive SHA256 mismatch")
    try:
        decoder = lzma.LZMADecompressor(
            format=lzma.FORMAT_XZ, memlimit=64 * 1024 * 1024
        )
        raw = decoder.decompress(data, max_length=MAX_TAR_BYTES + 1)
        if len(raw) > MAX_TAR_BYTES:
            raise ValueError("decompressed archive size limit exceeded")
        if not decoder.eof or decoder.unused_data:
            raise ValueError("invalid compressed archive framing")
        selected: dict[str, bytes] = {}
        required = {"7zzs", "License.txt", "readme.txt"}
        seen: set[str] = set()
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive:
                if len(seen) >= MAX_MEMBERS:
                    raise ValueError("archive member count limit exceeded")
                name = member.name
                canonical = name.removesuffix("/") if member.isdir() else name
                if (
                    not canonical
                    or "\\" in name
                    or any(part in {"", ".", ".."} for part in canonical.split("/"))
                ):
                    raise ValueError("unsafe archive member name")
                if canonical in seen:
                    raise ValueError("duplicate archive member")
                seen.add(canonical)
                # Reject extensions/sparse records as well as links and devices.
                header_type = raw[member.offset + 156 : member.offset + 157]
                if (
                    header_type
                    not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}
                    or not (member.isfile() or member.isdir())
                    or member.pax_headers
                    or member.sparse is not None
                ):
                    raise ValueError("unsupported archive member type")
                if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                    raise ValueError("archive member size limit exceeded")
                if name not in required:
                    continue
                if not member.isfile() or (member.size == 0 and name != "7zzs"):
                    raise ValueError(
                        "required archive member is not a nonempty regular file"
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("missing archive member data")
                with stream:
                    content = stream.read(MAX_MEMBER_BYTES + 1)
                if len(content) != member.size:
                    raise ValueError("archive member length mismatch")
                selected[name] = content
        if selected.keys() != required:
            raise ValueError("missing required archive members")
    except (lzma.LZMAError, tarfile.TarError, EOFError) as exc:
        raise ValueError("invalid upstream archive") from exc
    binary = selected["7zzs"]
    if (
        len(binary) < 64
        or binary[:7] != b"\x7fELF\x02\x01\x01"
        or struct.unpack_from("<HHI", binary, 16)
        not in {(2, pin.machine, 1), (3, pin.machine, 1)}
        or struct.unpack_from("<H", binary, 52)[0] != 64
    ):
        raise ValueError("invalid or mismatched ELF64 executable")
    return selected


def install(architecture: str, output: Path) -> None:
    """Write only beneath a new, non-symlink staging root after full validation."""
    pin = pin_for_arch(architecture)
    if output.exists() or output.is_symlink():
        raise ValueError("output tree must not already exist")
    if ".." in output.parts or any(
        parent.is_symlink() for parent in output.absolute().parents
    ):
        raise ValueError("output parents must not be symlinks or traversal paths")
    files = unpack_archive(download_archive(pin), pin)
    binary_hash = hashlib.sha256(files["7zzs"]).hexdigest()
    provenance = {
        "name": "7-Zip",
        "version": VERSION,
        "architecture": architecture,
        "source_url": SOURCE_URL,
        "download_url": pin.url,
        "archive_sha256": pin.sha256,
        "binary_sha256": binary_hash,
        "binary_path": "/usr/bin/7zz",
        "upstream_binary": "7zzs",
    }
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "components": [
            {
                "type": "application",
                "name": "7-Zip",
                "version": VERSION,
                "purl": f"pkg:generic/7-zip@{VERSION}",
                "cpe": f"cpe:2.3:a:7-zip:7-zip:{VERSION}:*:*:*:*:*:*:*",
                "description": (
                    "Maintainer-declared metadata for the pinned upstream standalone "
                    "binary; not auto-discovered."
                ),
                "bom-ref": f"7zip-{VERSION}-{architecture}",
                "hashes": [{"alg": "SHA-256", "content": binary_hash}],
                "licenses": [
                    {"license": {"id": "LGPL-2.1-or-later"}},
                    {"license": {"id": "BSD-3-Clause"}},
                    {"license": {"id": "BSD-2-Clause"}},
                    {
                        "license": {
                            "name": "unRAR restriction (complete terms in License.txt)"
                        }
                    },
                ],
                "externalReferences": [
                    {
                        "type": "distribution",
                        "url": pin.url,
                        "hashes": [{"alg": "SHA-256", "content": pin.sha256}],
                    },
                    {"type": "website", "url": SOURCE_URL},
                ],
            }
        ],
    }
    output.mkdir(mode=0o755)
    try:
        binary_dir = output / "usr/bin"
        doc = output / "usr/share/doc/7zip"
        legacy = output / "usr/share/doc/7zip-rar"
        for directory in (binary_dir, doc, legacy):
            directory.mkdir(parents=True, mode=0o755)
        binary = binary_dir / "7zz"
        _ = binary.write_bytes(files["7zzs"])
        binary.chmod(0o755)
        (binary_dir / "7z").symlink_to("7zz")
        for name in ("License.txt", "readme.txt"):
            _ = (doc / name).write_bytes(files[name])
        _ = (legacy / "copyright").write_bytes(
            b"Upstream 7-Zip notice compatibility path; not a Debian package.\n\n"
            + files["License.txt"]
        )
        for name, document in (
            ("provenance.json", provenance),
            ("7zip.cdx.json", sbom),
        ):
            _ = (doc / name).write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    except BaseException:
        shutil.rmtree(output)
        raise


class Arguments(argparse.Namespace):
    arch: str = ""
    output: Path = Path(".")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--arch", required=True)
    _ = parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv, namespace=Arguments())
    try:
        install(args.arch, args.output)
    except (ValueError, OSError, urllib.error.URLError):
        # Network errors can contain credentials or environment-specific URLs.
        print(
            "7-Zip staging failed: invalid input, download, archive, or output",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
