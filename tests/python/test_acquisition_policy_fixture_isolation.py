"""Acquisition policy fixtures must not leak encryption state to later tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_CASE = (
    "tests/python/test_acquisition_policy_395.py::"
    "test_untracked_acceptance_is_constrained_even_for_manual_selection"
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=_ROOT,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                (str(_ROOT / "app"), str(_ROOT / "tests/python"))
            ),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_policy_case_preserves_unchanged_api_key_plaintext_control() -> None:
    result = _run(
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        _CASE,
        "tests/python/test_api_key_middleware.py::"
        "test_ensure_api_key_replaces_blank_value",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("cipher_loaded", [False, True])
def test_policy_fixture_restores_exact_prior_cipher_cache(cipher_loaded: bool) -> None:
    result = _run(
        "-c",
        """
import sys
import conftest
import pytest
import security
from cryptography.fernet import Fernet

prior = Fernet(Fernet.generate_key()) if sys.argv[1] == 'loaded' else None
security._SECRET_CIPHER = prior
assert pytest.main(['-p', 'no:cacheprovider', '-q', sys.argv[2]]) == 0
assert security._SECRET_CIPHER is prior, 'policy fixture leaked its cipher cache'
""",
        "loaded" if cipher_loaded else "unloaded",
        _CASE,
    )
    assert result.returncode == 0, result.stdout + result.stderr
