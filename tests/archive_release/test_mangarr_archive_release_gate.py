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


def test_runtime_uses_pinned_standalone_with_bound_provenance(
    runtime: dict[str, Any],
) -> None:
    metadata = runtime["archive_runtime"]
    provenance = metadata["provenance"]
    pins = {
        "amd64": (
            "2b539cf067b2ad48e1baa5093dbaa8818b73542d1222a0ffde9dc5f2922ea62e",
            "0b58a64e71ebdf53751cd954cad1cc552c6a11ef51336bee6acafbdc54c28c1f",
            "7z2606-linux-x64.tar.xz",
        ),
        "arm64": (
            "edcc408aacce77da5223728f2eceb5e9f6dc713b8a9df934ad21e2a7f7560627",
            "265e45b5847392c0cd97a84acb6b3f9933c3e43d698fb5a2979f5e9a80c05758",
            "7z2606-linux-arm64.tar.xz",
        ),
    }
    archive_hash, binary_hash, filename = pins[provenance["architecture"]]
    assert metadata["version_returncode"] == 0
    assert "7-Zip (z) 26.06" in metadata["version_banner"]
    assert metadata["alias_matches"] is True
    assert metadata["binary_sha256"] == binary_hash == provenance["binary_sha256"]
    assert provenance["archive_sha256"] == archive_hash
    assert provenance["version"] == "26.06" and provenance["upstream_binary"] == "7zzs"
    assert provenance["source_url"] == "https://github.com/ip7z/7zip/releases/tag/26.06"
    assert provenance["download_url"] == (
        f"https://github.com/ip7z/7zip/releases/download/26.06/{filename}"
    )
    assert runtime["packages"]["returncode"] != 0
    assert "install ok installed" not in runtime["packages"]["stdout"]


def test_runtime_installs_reviewed_rarfile_security_pin(
    runtime: dict[str, Any],
) -> None:
    assert runtime["rarfile"] == "4.5"
