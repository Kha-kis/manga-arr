"""Packaging contracts; synthetic ZIPs are not built-wheel qualification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import cast
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MODULES = frozenset(
    """auth backups cli qbit_auth release_identity release_selections shared version
    file_identity_cli file_identity_recovery file_identity_consumers
    file_identity_store file_identity file_mutation_lock download_identity
    private_file_claim private_pack_claim import_plan acquisition_policy events
    parsing files import_kinds import_lease rescan comicinfo helpers
    import_pack_cleanup""".split()
)


def test_build_backend_has_reviewed_security_pin() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    build_system = cast(dict[str, object], project["build-system"])
    # GHSA-xmj3-9gf7-h52w: 84.1.0 is the first patched setuptools release.
    assert build_system["requires"] == ["setuptools==84.1.0"]
    assert build_system["build-backend"] == "setuptools.build_meta"


def test_explicit_operator_module_graph() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    modules = cast(list[str], project["tool"]["setuptools"]["py-modules"])
    assert len(modules) == len(MODULES) and set(modules) == MODULES
    assert project["project"]["scripts"] == {"mangarr": "cli:main"}


def test_fast_qualifies_one_verified_wheel_in_existing_step() -> None:
    workflow = cast(
        dict[str, dict[str, dict[str, object]]],
        yaml.safe_load((ROOT / ".github/workflows/pr-fast.yml").read_text()),
    )
    steps = cast(list[dict[str, object]], workflow["jobs"]["fast"]["steps"])
    step = next(
        s["run"] for s in steps if s["name"] == "Validate package metadata and CLI"
    )
    assert isinstance(step, str)
    assert step.count("python -m pip wheel --no-deps . --wheel-dir") == 1
    assert "python -m pip install --no-deps ." not in step
    assert step.index("verify_operator_wheel.py") < step.index("python -m pip install")
    for value in (
        "RUNNER_TEMP",
        "mktemp -d",
        '"${wheels[0]}"',
        "MANGARR_INSTALLED_CLI",
        "MANGARR_OPERATOR_WHEEL",
        "test_operator_ownership_cli.py",
        "test_operator_cli_packaging.py",
        "test_operator_cli.py",
        "${#wheels[@]}",
    ):
        assert value in step
    assert workflow["jobs"]["fast"]["timeout-minutes"] == 5
    assert (
        steps[-1]["run"]
        == 'MANGARR_INSTALLED_CLI="$(command -v mangarr)" make test-fast'
    )


def verify(directory: Path) -> subprocess.CompletedProcess[str]:
    helper = ROOT / "scripts" / "verify_operator_wheel.py"
    assert helper.is_file(), (
        "wheel verifier API is absent (not an actual wheel failure)"
    )
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(helper),
            str(directory),
            str(ROOT / "pyproject.toml"),
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def synthetic_wheel(
    directory: Path, *, missing: str = "", entry: str = "cli:main"
) -> Path:
    wheel = directory / "mangarr-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for module in sorted(MODULES - {missing}):
            archive.writestr(module + ".py", b"# synthetic unit fixture\n")
        archive.writestr(
            "mangarr-0.0.0.dist-info/METADATA", "Name: mangarr\nVersion: 0.0.0\n"
        )
        archive.writestr(
            "mangarr-0.0.0.dist-info/entry_points.txt",
            f"[console_scripts]\nmangarr = {entry}\n",
        )
    return wheel


def test_synthetic_inventory_binds_digest(tmp_path: Path) -> None:
    wheel = synthetic_wheel(tmp_path)
    result = verify(tmp_path)
    assert result.returncode == 0, result.stderr
    report = cast(dict[str, object], json.loads(result.stdout))
    assert report["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert report["wheel"] == str(wheel)
    assert set(cast(dict[str, str], report["modules"])) == MODULES


@pytest.mark.parametrize(
    "fault", ["missing", "entry", "duplicate", "extra", "multiple", "empty"]
)
def test_synthetic_verifier_refuses(tmp_path: Path, fault: str) -> None:
    if fault != "empty":
        wheel = synthetic_wheel(
            tmp_path,
            missing="comicinfo" if fault == "missing" else "",
            entry="other:main" if fault == "entry" else "cli:main",
        )
        if fault in {"duplicate", "extra"}:
            with zipfile.ZipFile(wheel, "a") as archive:
                if fault == "duplicate":
                    with pytest.warns(UserWarning, match="Duplicate name"):
                        archive.writestr("cli.py", b"duplicate")
                else:
                    archive.writestr("schema.py", b"unapproved")
        if fault == "multiple":
            _ = (tmp_path / "other.whl").write_bytes(wheel.read_bytes())
    result = verify(tmp_path)
    assert result.returncode != 0
    assert result.stdout == ""


def test_selected_actual_wheel_removed_module_guard(tmp_path: Path) -> None:
    selected = os.environ.get("MANGARR_OPERATOR_WHEEL")
    if selected is None:
        assert "MANGARR_INSTALLED_CLI" not in os.environ, "selected wheel is required"
        pytest.skip("actual built wheel is required; synthetic tests do not qualify it")
    assert os.environ.get("MANGARR_INSTALLED_CLI"), "installed launcher is required"
    wheel = Path(selected)
    assert wheel.is_absolute() and wheel.is_file()
    assert verify(wheel.parent).returncode == 0
    damaged = tmp_path / wheel.name
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(damaged, "w") as target:
        for info in source.infolist():
            if info.filename != "comicinfo.py":
                target.writestr(info, source.read(info))
    result = verify(tmp_path)
    assert result.returncode != 0 and result.stdout == ""


def test_installed_lane_cannot_skip_missing_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MANGARR_INSTALLED_CLI", "/selected/mangarr")
    monkeypatch.delenv("MANGARR_OPERATOR_WHEEL", raising=False)
    try:
        with pytest.raises(AssertionError, match="selected wheel is required"):
            test_selected_actual_wheel_removed_module_guard(tmp_path)
    except pytest.skip.Exception:
        pytest.fail("explicit installed lane skipped its required wheel")


def test_installed_lane_rejects_wrong_wheel_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = synthetic_wheel(tmp_path, entry="other:main")
    monkeypatch.setenv("MANGARR_INSTALLED_CLI", "/selected/mangarr")
    monkeypatch.setenv("MANGARR_OPERATOR_WHEEL", str(wheel))
    with pytest.raises(AssertionError):
        test_selected_actual_wheel_removed_module_guard(tmp_path)
