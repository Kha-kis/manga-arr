"""Explicit image gate; not part of the host-only Python suite."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest


@pytest.fixture(scope="module")
def decoded() -> dict[str, Any]:
    image = os.environ["MANGARR_ARCHIVE_GATE_IMAGE"]
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--memory=512m",
            "--memory-swap=512m",
            "--cpus=1",
            "--user=1000:1000",
            "--tmpfs=/tmp:rw,nosuid,nodev,mode=1777,size=64m",
            f"--mount=type=bind,src={root},dst=/work,readonly",
            "-e=PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint=python",
            image,
            "/work/tests/archive_release/decoder_probe.py",
        ],
        capture_output=True,
        text=True,
        timeout=40,
        check=True,
    )
    return json.loads(completed.stdout)


def test_fixture_is_genuine_compressed_multipart(decoded: dict[str, Any]) -> None:
    assert decoded["multipart_compressed"] is True
    assert decoded["multipart_volumes"] == 5


def test_existing_cli_decodes_all_multipart_bytes(decoded: dict[str, Any]) -> None:
    assert decoded["multipart_cli"]["returncode"] == 0, decoded["multipart_cli"]
    assert decoded["multipart_cli_matches"] is True


def test_existing_rarfile_reads_all_compressed_multipart_bytes(
    decoded: dict[str, Any],
) -> None:
    assert decoded.get("multipart_read_matches") is True, decoded.get("multipart_error")


def test_missing_required_volume_is_not_success(decoded: dict[str, Any]) -> None:
    assert decoded["missing_volume_cli"]["returncode"] != 0
    assert decoded.get("missing_volume_rejected")


def test_existing_cbz_and_7z_codec_controls(decoded: dict[str, Any]) -> None:
    assert decoded["7z_create"]["returncode"] == 0
    assert decoded["7z_extract"]["returncode"] == 0
    assert decoded["7z_bytes_match"] is True
    assert decoded["cbz_extract"]["returncode"] == 0
    assert decoded["cbz_bytes_match"] is True


def test_actual_cbr_conversion_keeps_original_and_page_bytes(
    decoded: dict[str, Any],
) -> None:
    assert decoded["conversion_matches"] is True
    assert decoded["conversion_preserves_original"] is True


def test_packaged_rar_license_notices_are_retained(decoded: dict[str, Any]) -> None:
    assert decoded["plugin_license_retained"] is True
