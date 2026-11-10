"""Parser allocation bounds tested through public APIs without large allocations."""

from __future__ import annotations

import io
from pathlib import Path
import shlex
import struct
import subprocess
from zlib import crc32

import pytest
import rarfile


COMMENT_LIMIT = 256 * 1024
ROOT = Path(__file__).resolve().parents[2]


class RecordingReader(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.requests: list[int] = []

    def read(self, size: int | None = -1) -> bytes:
        size = -1 if size is None else size
        self.requests.append(size)
        if size > COMMENT_LIMIT:
            raise AssertionError(f"untrusted archive requested oversized read: {size}")
        return super().read(size)


def test_recording_reader_accepts_none_and_keeps_oversized_guard() -> None:
    payload = b"bounded reader positive control"
    with RecordingReader(payload) as reader:
        assert reader.read(None) == payload
        assert reader.requests == [-1]
        with pytest.raises(AssertionError, match="oversized read"):
            reader.read(COMMENT_LIMIT + 1)
        assert reader.requests == [-1, COMMENT_LIMIT + 1]


def _rar3_block(kind: int, flags: int, body: bytes) -> bytes:
    header = struct.pack("<BHH", kind, flags, 7 + len(body)) + body
    return struct.pack("<H", crc32(header) & 0xFFFF) + header


def _vint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _rar5_block(body: bytes) -> bytes:
    header = _vint(len(body)) + body
    return struct.pack("<I", crc32(header)) + header


def _rar5_comment(payload: bytes, packed_size: int, file_size: int) -> bytes:
    # Stored CMT service block; large declared sizes never imply large fixture data.
    service = (
        b"".join(
            _vint(value) for value in (3, 2, packed_size, 0, file_size, 0o600, 0, 1, 3)
        )
        + b"CMT"
    )
    return (
        b"Rar!\x1a\x07\x01\x00"
        + _rar5_block(b"\x01\x00\x00")
        + _rar5_block(service)
        + payload
    )


@pytest.mark.parametrize("subtype", [0x100, 0x101, 0x102])
def test_old_sub_declared_payload_is_not_read_for_header_crc(subtype: int) -> None:
    header = _rar3_block(0x73, 0, b"\x00" * 6)
    old_sub = _rar3_block(0x77, 0x8000, struct.pack("<IHB", 0xFFFFFFFF, subtype, 0))
    with RecordingReader(b"Rar!\x1a\x07\x00" + header + old_sub) as reader:
        with rarfile.RarFile(reader) as archive:
            assert archive.namelist() == []
        assert 0xFFFFFFFF not in reader.requests


@pytest.mark.parametrize("sizes", [(1, COMMENT_LIMIT + 1), (COMMENT_LIMIT + 1, 1)])
def test_oversized_comment_is_skipped_before_read(sizes: tuple[int, int]) -> None:
    with RecordingReader(_rar5_comment(b"c", *sizes)) as reader:
        with rarfile.RarFile(reader) as archive:
            assert archive.comment is None
        assert max(reader.requests) <= COMMENT_LIMIT


@pytest.mark.parametrize("payload", [b"comment", b"c" * COMMENT_LIMIT])
def test_bounded_comment_positive_control(payload: bytes) -> None:
    with RecordingReader(_rar5_comment(payload, len(payload), len(payload))) as reader:
        with rarfile.RarFile(reader) as archive:
            assert archive.comment == payload.decode("ascii")
        assert max(reader.requests) <= COMMENT_LIMIT


def _runtime_package_step(dockerfile: str) -> str:
    instructions = [
        line.strip()
        for line in dockerfile.replace("\\\n", " ").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    stage_start = max(
        index
        for index, instruction in enumerate(instructions)
        if instruction.startswith("FROM ")
    )
    package_steps = [
        instruction[4:]
        for instruction in instructions[stage_start + 1 :]
        if instruction.startswith("RUN ") and "apt-get update" in instruction
    ]
    assert len(package_steps) == 1, "expected one final-stage package instruction"
    return package_steps[0]


@pytest.mark.parametrize("components", ["main", "main non-free", "main contrib", ""])
def test_debian_component_edit_is_exact_and_preserves_signed_sources(
    tmp_path: Path, components: str
) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    package_step = _runtime_package_step(dockerfile)
    assert "sed -i" in package_step, (
        "signed Debian sources must gain the RAR codec component"
    )
    edit, separator, _ = package_step.partition(" && apt-get update")
    assert separator and "apt-get" not in edit
    original = "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie trixie-updates\n"
    original += f"Components: {components}\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
    sources = tmp_path / "debian.sources"
    sources.write_text(original)
    completed = subprocess.run(
        [
            "sh",
            "-c",
            edit.replace(
                "/etc/apt/sources.list.d/debian.sources", shlex.quote(str(sources))
            ),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if components == "main":
        assert completed.returncode == 0, completed.stderr
        assert sources.read_text() == original.replace(
            "Components: main\n", "Components: main non-free\n"
        )
    else:
        assert completed.returncode != 0
        assert sources.read_text() == original


def test_runtime_requirement_pins_reviewed_security_release() -> None:
    assert "rarfile==4.5" in (ROOT / "requirements.txt").read_text().splitlines()


@pytest.mark.parametrize(
    "builder_step", ["sed -i builder-decoy && apt-get update", "echo builder-decoy"]
)
def test_runtime_package_step_ignores_builder_decoy(builder_step: str) -> None:
    runtime_step = "sed -i runtime-edit && apt-get update"
    dockerfile = (
        f"FROM python AS builder\nRUN {builder_step}\n\n"
        "FROM python AS runtime\nRUN echo unrelated-runtime-step\n"
        f"RUN {runtime_step}\n\nRUN python -V\n"
    )
    assert _runtime_package_step(dockerfile) == runtime_step


def test_runtime_package_step_requires_final_stage_package_instruction() -> None:
    dockerfile = (
        "FROM python AS builder\nRUN sed -i builder-decoy && apt-get update\n\n"
        "FROM python AS runtime\nRUN python -V\n"
    )
    with pytest.raises(AssertionError, match="one final-stage package instruction"):
        _runtime_package_step(dockerfile)


def test_runtime_package_step_keeps_single_stage_selection() -> None:
    step = "sed -i runtime-edit && apt-get update"
    assert _runtime_package_step(f"FROM python\nRUN {step}\n\n") == step


def test_runtime_component_edit_cannot_borrow_signed_builder_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    runtime = dockerfile.rsplit("\nFROM ", 1)[1]
    signed_step = runtime.split("RUN ", 1)[1].split("\n\n", 1)[0]
    (tmp_path / "Dockerfile").write_text(
        f"FROM python AS builder\nRUN {signed_step}\n\n"
        "FROM python AS runtime\nRUN apt-get update\n\n"
    )
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="signed Debian sources must gain"):
        test_debian_component_edit_is_exact_and_preserves_signed_sources(
            tmp_path, "main"
        )
