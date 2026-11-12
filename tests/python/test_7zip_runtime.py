"""Offline contracts for the pinned build-only standalone archive tool."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import lzma
from pathlib import Path
import re
import struct
import subprocess
import sys
import tarfile
from types import ModuleType
from email.message import Message
from urllib.request import Request

import pytest


ROOT = Path(__file__).resolve().parents[2]
LICENSE = b"Full upstream license\nGNU LGPL\nunRAR restriction\nEND\n"
README = b"Full upstream readme\n7-Zip 26.06\nEND\n"


@pytest.fixture
def installer() -> ModuleType:
    path = ROOT / "scripts/install_7zip.py"
    assert path.is_file(), "The build-only pinned archive installer is missing"
    spec = importlib.util.spec_from_file_location("install_7zip", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def elf(machine: int = 62) -> bytes:
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", header, 16, 2, machine, 1)
    struct.pack_into("<H", header, 52, 64)
    return bytes(header) + b"synthetic executable; never run"


def archive(entries: list[tuple[str, bytes, bytes]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, payload, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(payload)
            member.linkname = (
                "7zzs" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            )
            tar.addfile(member, io.BytesIO(payload))
    return lzma.compress(stream.getvalue())


def entries(binary: bytes | None = None) -> list[tuple[str, bytes, bytes]]:
    return [
        ("7zzs", elf() if binary is None else binary, tarfile.REGTYPE),
        ("License.txt", LICENSE, tarfile.REGTYPE),
        ("readme.txt", README, tarfile.REGTYPE),
    ]


def test_docker_replaces_affected_debian_archive_packages() -> None:
    source = (ROOT / "Dockerfile").read_text()
    commands = source.replace("\\\n", " ")
    installs = re.findall(r"apt-get\s+install\b[^\n;&]*", commands)
    assert not any(re.search(r"\b7zip(?:-rar)?\b", line) for line in installs), (
        "The runtime still installs affected Debian 7zip/7zip-rar packages"
    )
    assert any("ca-certificates" in line for line in installs)
    assert "AS archive-fetcher" in source
    assert "ARG TARGETARCH" in source
    assert "scripts/install_7zip.py" in source
    assert '--arch "$TARGETARCH" --output /out' in source
    assert "apt-get" not in source.split("\nFROM ", 1)[0]
    assert "COPY --from=archive-fetcher /out/ /" in source
    bases = re.findall(r"^FROM (python:[^\s]+)", source, re.MULTILINE)
    assert len(bases) == 3 and len(set(bases)) == 1
    assert "@sha256:" in bases[0]
    context = (ROOT / ".dockerignore").read_text().splitlines()
    assert "!scripts/" in context and "scripts/*" in context
    assert "!scripts/install_7zip.py" in context


@pytest.mark.parametrize(
    ("arch", "filename", "digest", "machine"),
    [
        (
            "amd64",
            "7z2606-linux-x64.tar.xz",
            "2b539cf067b2ad48e1baa5093dbaa8818b73542d1222a0ffde9dc5f2922ea62e",
            62,
        ),
        (
            "arm64",
            "7z2606-linux-arm64.tar.xz",
            "edcc408aacce77da5223728f2eceb5e9f6dc713b8a9df934ad21e2a7f7560627",
            183,
        ),
    ],
)
def test_exact_release_pins(
    installer: ModuleType, arch: str, filename: str, digest: str, machine: int
) -> None:
    pin = installer.pin_for_arch(arch)
    assert installer.VERSION == "26.06"
    assert pin.filename == filename and pin.sha256 == digest and pin.machine == machine
    assert pin.url == f"https://github.com/ip7z/7zip/releases/download/26.06/{filename}"


@pytest.mark.parametrize(
    "arch", ["", "x64", "x86_64", "aarch64", "AMD64", "386", "../amd64"]
)
def test_architecture_is_strict(installer: ModuleType, arch: str) -> None:
    with pytest.raises(ValueError, match="architecture"):
        installer.pin_for_arch(arch)


def test_hash_is_checked_before_parsing(
    installer: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Unauthenticated bytes reached the archive parser")

    monkeypatch.setattr(installer.lzma, "LZMADecompressor", forbidden)
    with pytest.raises(ValueError, match="SHA256"):
        installer.unpack_archive(b"untrusted", installer.pin_for_arch("amd64"))


@pytest.mark.parametrize("arch,machine", [("amd64", 62), ("arm64", 183)])
def test_valid_archive_preserves_only_exact_members(
    installer: ModuleType, arch: str, machine: int
) -> None:
    payload = archive(
        entries(elf(machine))
        + [
            ("MANUAL/", b"", tarfile.DIRTYPE),
            ("MANUAL/help.txt", b"ignored", tarfile.REGTYPE),
        ]
    )
    pin = installer.ArchivePin(
        "synthetic.tar.xz", hashlib.sha256(payload).hexdigest(), machine
    )
    assert installer.unpack_archive(payload, pin) == {
        "7zzs": elf(machine),
        "License.txt": LICENSE,
        "readme.txt": README,
    }


@pytest.mark.parametrize(
    "name", ["../outside", "/absolute", "a/../7zzs", "./7zzs", "a\\7zzs", "a//b"]
)
def test_refuses_noncanonical_member_names(installer: ModuleType, name: str) -> None:
    payload = archive(entries() + [(name, b"bad", tarfile.REGTYPE)])
    pin = installer.ArchivePin(
        "synthetic.tar.xz", hashlib.sha256(payload).hexdigest(), 62
    )
    with pytest.raises(ValueError, match="member"):
        installer.unpack_archive(payload, pin)


@pytest.mark.parametrize(
    "kind",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.FIFOTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
    ],
)
@pytest.mark.parametrize("name", ["7zzs", "unused"])
def test_refuses_special_members(installer: ModuleType, kind: bytes, name: str) -> None:
    payload = archive(entries()[1:] + [(name, b"", kind)])
    pin = installer.ArchivePin(
        "synthetic.tar.xz", hashlib.sha256(payload).hexdigest(), 62
    )
    with pytest.raises(ValueError, match="member"):
        installer.unpack_archive(payload, pin)


@pytest.mark.parametrize("name", ["7zzs", "License.txt", "readme.txt", "unused"])
def test_refuses_duplicate_members(installer: ModuleType, name: str) -> None:
    extra = [(name, b"duplicate", tarfile.REGTYPE)]
    if name == "unused":
        extra *= 2
    payload = archive(entries() + extra)
    pin = installer.ArchivePin(
        "synthetic.tar.xz", hashlib.sha256(payload).hexdigest(), 62
    )
    with pytest.raises(ValueError, match="duplicate"):
        installer.unpack_archive(payload, pin)


@pytest.mark.parametrize(
    "binary",
    [
        b"",
        b"not elf",
        elf(183),
        b"\x7fELF\x01" + elf()[5:],
        b"\x7fELF\x02\x02" + elf()[6:],
        elf()[:40],
    ],
)
def test_refuses_invalid_or_wrong_architecture_elf(
    installer: ModuleType, binary: bytes
) -> None:
    payload = archive(entries(binary))
    pin = installer.ArchivePin(
        "synthetic.tar.xz", hashlib.sha256(payload).hexdigest(), 62
    )
    with pytest.raises(ValueError, match="ELF"):
        installer.unpack_archive(payload, pin)


@pytest.mark.parametrize("missing", ["7zzs", "License.txt", "readme.txt"])
def test_requires_all_selected_members(installer: ModuleType, missing: str) -> None:
    payload = archive([entry for entry in entries() if entry[0] != missing])
    pin = installer.ArchivePin(
        "synthetic.tar.xz", hashlib.sha256(payload).hexdigest(), 62
    )
    with pytest.raises(ValueError, match="missing"):
        installer.unpack_archive(payload, pin)


@pytest.mark.parametrize(
    "bound", ["MAX_ARCHIVE_BYTES", "MAX_TAR_BYTES", "MAX_MEMBER_BYTES", "MAX_MEMBERS"]
)
def test_bounds_archive_allocations(
    installer: ModuleType, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    payload = archive(entries())
    pin = installer.ArchivePin(
        "synthetic.tar.xz", hashlib.sha256(payload).hexdigest(), 62
    )
    monkeypatch.setattr(installer, bound, 1)
    with pytest.raises(ValueError, match="limit"):
        installer.unpack_archive(payload, pin)


@pytest.mark.parametrize(
    "payload",
    [
        b"not xz",
        lzma.compress(b"not tar"),
        lzma.compress(b""),
        archive(entries())[:-5],
        archive(entries()) + b"trailing",
    ],
)
def test_malformed_archives_fail_closed(installer: ModuleType, payload: bytes) -> None:
    pin = installer.ArchivePin(
        "synthetic.tar.xz", hashlib.sha256(payload).hexdigest(), 62
    )
    with pytest.raises(ValueError):
        installer.unpack_archive(payload, pin)


def test_install_emits_complete_notices_alias_and_provenance(
    installer: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = archive(entries())
    pin = installer.ArchivePin(
        "synthetic.tar.xz", hashlib.sha256(payload).hexdigest(), 62
    )
    monkeypatch.setitem(installer.PINS, "amd64", pin)
    monkeypatch.setattr(installer, "download_archive", lambda selected: payload)
    output = tmp_path / "out"
    installer.install("amd64", output)
    binary = output / "usr/bin/7zz"
    assert binary.read_bytes() == elf()
    assert binary.stat().st_mode & 0o777 == 0o755
    alias = output / "usr/bin/7z"
    assert alias.is_symlink() and alias.readlink() == Path("7zz")
    doc = output / "usr/share/doc/7zip"
    assert (doc / "License.txt").read_bytes() == LICENSE
    assert (doc / "readme.txt").read_bytes() == README
    legacy = output / "usr/share/doc/7zip-rar/copyright"
    assert LICENSE in legacy.read_bytes()
    assert b"not a Debian package" in legacy.read_bytes()
    provenance = json.loads((doc / "provenance.json").read_text())
    assert provenance["version"] == "26.06" and provenance["architecture"] == "amd64"
    assert provenance["archive_sha256"] == pin.sha256
    assert provenance["binary_sha256"] == hashlib.sha256(elf()).hexdigest()
    assert provenance["download_url"] == pin.url
    sbom = json.loads((doc / "7zip.cdx.json").read_text())
    assert sbom["bomFormat"] == "CycloneDX" and sbom["specVersion"] == "1.6"
    component = sbom["components"][0]
    assert component["name"] == "7-Zip" and component["version"] == "26.06"
    assert component["purl"] == "pkg:generic/7-zip@26.06"
    assert component["cpe"] == "cpe:2.3:a:7-zip:7-zip:26.06:*:*:*:*:*:*:*"
    assert "maintainer-declared" in component["description"].lower()
    assert "not auto-discovered" in component["description"]
    assert {"alg": "SHA-256", "content": provenance["binary_sha256"]} in component[
        "hashes"
    ]
    assert "unRAR" in json.dumps(component["licenses"])
    assert {"license": {"id": "BSD-2-Clause"}} in component["licenses"]
    assert not list(output.rglob("Rar.so"))


@pytest.mark.parametrize("existing", ["directory", "file", "symlink", "dangling"])
def test_output_must_be_exclusively_new(
    installer: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    existing: str,
) -> None:
    output = tmp_path / "out"
    if existing == "directory":
        output.mkdir()
    elif existing == "file":
        output.write_bytes(b"keep")
    else:
        output.symlink_to(tmp_path if existing == "symlink" else tmp_path / "missing")

    def forbidden(selected: object) -> bytes:
        pytest.fail("Existing output must be refused before network access")

    monkeypatch.setattr(installer, "download_archive", forbidden)
    with pytest.raises((ValueError, FileExistsError)):
        installer.install("amd64", output)


def test_invalid_input_leaves_no_output(
    installer: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(installer, "download_archive", lambda selected: b"tampered")
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="SHA256"):
        installer.install("amd64", output)
    assert not output.exists()


def test_cli_rejects_unknown_arch_without_output(
    installer: ModuleType, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    assert installer.main(["--arch", "x86_64", "--output", str(output)]) == 1
    assert not output.exists()


class Response(io.BytesIO):
    def __init__(self, payload: bytes, url: str, length: str | None = None) -> None:
        super().__init__(payload)
        self.url = url
        self.headers = Message()
        if length is not None:
            self.headers["Content-Length"] = length

    def geturl(self) -> str:
        return self.url


@pytest.mark.parametrize("length", [None, "3"])
def test_download_is_bounded_and_uses_pinned_https(
    installer: ModuleType, monkeypatch: pytest.MonkeyPatch, length: str | None
) -> None:
    pin = installer.pin_for_arch("amd64")

    class Opener:
        def open(self, request: Request, *, timeout: float) -> Response:
            assert request.full_url == pin.url
            assert 0 < timeout <= 30
            return Response(b"abc", pin.url, length)

    monkeypatch.setattr(
        installer.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    assert installer.download_archive(pin) == b"abc"


@pytest.mark.parametrize(
    "length,payload,url",
    [
        ("-1", b"", "https://github.com"),
        ("invalid", b"", "https://github.com"),
        ("100", b"abc", "https://github.com"),
        (None, b"abc", "http://github.com"),
        (None, b"abc", "https://user:secret@github.com"),
    ],
)
def test_download_rejects_bad_length_and_transport(
    installer: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    length: str | None,
    payload: bytes,
    url: str,
) -> None:
    class Opener:
        def open(self, request: Request, *, timeout: float) -> Response:
            return Response(payload, url, length)

    monkeypatch.setattr(
        installer.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    monkeypatch.setattr(installer, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(ValueError):
        installer.download_archive(installer.pin_for_arch("amd64"))


def test_download_bounds_stream_without_content_length(
    installer: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = installer.pin_for_arch("amd64")

    class Opener:
        def open(self, request: Request, *, timeout: float) -> Response:
            return Response(b"abcd", pin.url)

    monkeypatch.setattr(
        installer.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    monkeypatch.setattr(installer, "MAX_ARCHIVE_BYTES", 3)
    with pytest.raises(ValueError, match="limit"):
        installer.download_archive(pin)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/asset",
        "file:///etc/passwd",
        "https://user:secret@github.com/asset",
    ],
)
def test_redirect_handler_refuses_insecure_destinations(
    installer: ModuleType, url: str
) -> None:
    handler = installer.HTTPSRedirectHandler()
    with pytest.raises(ValueError, match="HTTPS"):
        handler.redirect_request(
            Request("https://github.com/asset"), None, 302, "redirect", Message(), url
        )


def test_symlinked_output_parent_refused_before_network(
    installer: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)

    def forbidden(selected: object) -> bytes:
        pytest.fail("Symlinked output parent must be refused before download")

    monkeypatch.setattr(installer, "download_archive", forbidden)
    with pytest.raises(ValueError, match="output"):
        installer.install("amd64", link / "out")
    assert not (tmp_path / "out").exists()


def test_runtime_probe_records_actual_standalone_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = ROOT / "tests/archive_release/mangarr_archive_runtime_probe.py"
    spec = importlib.util.spec_from_file_location("archive_runtime_probe_test", path)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    binary = tmp_path / "usr/bin/7zz"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(elf())
    (binary.parent / "7z").symlink_to("7zz")
    doc = tmp_path / "usr/share/doc/7zip"
    doc.mkdir(parents=True)
    provenance = {
        "version": "26.06",
        "binary_sha256": hashlib.sha256(elf()).hexdigest(),
    }
    (doc / "provenance.json").write_text(json.dumps(provenance))
    monkeypatch.setattr(probe, "Path", lambda value: tmp_path / value.lstrip("/"))

    def version(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert arguments == [str(binary)]
        return subprocess.CompletedProcess(
            arguments, 0, "\n7-Zip (z) 26.06 (x64)\n", ""
        )

    monkeypatch.setattr(probe.subprocess, "run", version)
    assert hasattr(probe, "archive_runtime_metadata"), (
        "Pinned standalone metadata probe is missing"
    )
    metadata = probe.archive_runtime_metadata()
    assert metadata["binary_sha256"] == hashlib.sha256(elf()).hexdigest()
    assert metadata["alias_matches"] is True
    assert metadata["version_returncode"] == 0 and "26.06" in metadata["version_banner"]
    assert metadata["provenance"] == provenance
