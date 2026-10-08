"""Tests-first archive release proposal: current runtime failures are intentional."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

IMAGE = os.environ.get(
    "MANGARR_ARCHIVE_GATE_IMAGE",
    "sha256:4f0d35b36ed47cff10bca6752e69c8db851e00969260b3443ed4272c91a71582",
)


@pytest.fixture(scope="module")
def runtime() -> dict[str, Any]:
    probe = Path(__file__).with_name("mangarr_archive_runtime_probe.py")
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
            f"--mount=type=bind,src={probe},dst=/probe.py,readonly",
            "-e=PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint=python",
            IMAGE,
            "/probe.py",
        ],
        capture_output=True,
        text=True,
        timeout=40,
        check=True,
    )
    return json.loads(completed.stdout)


def test_valid_real_rar_headers_are_not_a_decoder_capability_claim(
    runtime: dict[str, Any],
) -> None:
    assert (
        runtime["fixture_sha256"]
        == "96cd790a41d788e95597fc25a236c81c698c9ca4a95e2a99c3b5dd0edf66f9db"
    )
    assert runtime["rar_headers"]["returncode"] == 0
    assert (
        runtime["rarfile_header"]["compress_type"]
        != runtime["rarfile_header"]["stored_type"]
    )
    assert runtime["rarfile_header"]["file_size"] == runtime["expected_page_size"]


def test_existing_zip_codec_positive_control(runtime: dict[str, Any]) -> None:
    assert runtime["zip_extract"]["returncode"] == 0
    assert runtime["zip_bytes_match"] is True


def test_runtime_cli_decodes_known_compressed_rar_payload(
    runtime: dict[str, Any],
) -> None:
    assert runtime["rar_extract"]["returncode"] == 0, runtime["rar_extract"]["stderr"]
    assert runtime["rar_bytes_match"] is True


def test_runtime_rarfile_uses_existing_7z_to_read_compressed_payload(
    runtime: dict[str, Any],
) -> None:
    assert runtime.get("rarfile_bytes_match") is True, runtime.get("rarfile_read_error")


def test_runtime_keeps_the_rar_codec_plugin(runtime: dict[str, Any]) -> None:
    assert runtime["rar_codec_present"] is True


def test_runtime_installs_reviewed_rarfile_security_pin(
    runtime: dict[str, Any],
) -> None:
    assert runtime["rarfile"] == "4.5"
