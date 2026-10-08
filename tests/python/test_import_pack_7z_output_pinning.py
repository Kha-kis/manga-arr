"""Real extractor placement, independent of the pending pack/guard adapter.

Use the existing helper's x/-y/-o command shape with a generated ZIP fixture.
This tests 7z's output-path semantics, not its split-RAR decoder. No SQLite
writer or production state is involved. The public-path case is a negative
control: it must demonstrate why ordinary pathname output is not authority.
"""

from __future__ import annotations

import io
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

_PAGE_SIZE = 256 << 20
_FIRST = "started/Pack Series c001.cbz"
_LATER = "not-yet-created/Pack Series c002.cbz"


def _write_nested_archive(source: Path) -> None:
    # Stream the compressible fixture: no 256 MiB in-memory allocation.
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as outer:
        with outer.open(_FIRST, "w") as payload:
            with zipfile.ZipFile(payload, "w", zipfile.ZIP_STORED) as inner:
                with inner.open("001.jpg", "w") as page:
                    chunk = b"\0" * (1 << 20)
                    for _ in range(_PAGE_SIZE // len(chunk)):
                        page.write(chunk)
        later = io.BytesIO()
        with zipfile.ZipFile(later, "w") as inner:
            inner.writestr("002.jpg", b"later-page")
        outer.writestr(_LATER, later.getvalue())


def _pause_after_output_starts(child: subprocess.Popen[str], output: Path) -> int:
    deadline = time.monotonic() + 10
    primary = output / _FIRST
    while time.monotonic() < deadline:
        if child.poll() is not None:
            stdout, stderr = child.communicate(timeout=5)
            pytest.fail(f"extractor exited before pause: {stdout}\n{stderr}")
        if primary.exists() and primary.stat().st_size >= 4 << 20:
            child.send_signal(signal.SIGSTOP)
            break
        time.sleep(0.001)
    else:
        pytest.fail("extractor never started its first payload")
    while time.monotonic() < deadline:
        status = Path(f"/proc/{child.pid}/status").read_text()
        if any(
            line.startswith("State:") and line.split()[1] == "T"
            for line in status.splitlines()
        ):
            size = primary.stat().st_size
            assert 4 << 20 <= size < _PAGE_SIZE, "pause must occur during output"
            assert not (output / _LATER).exists(), "later path already opened"
            return size
        time.sleep(0.001)
    pytest.fail("extractor did not stop")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux proc-FD output contract")
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "public-control"])
def test_real_7z_later_entries_follow_output_authority(
    tmp_path: Path, pinned: bool
) -> None:
    extractor = shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
    if extractor is None:
        pytest.skip("real 7z required for extractor placement qualification")
    source = tmp_path / "nested.zip"
    _write_nested_archive(source)
    output = tmp_path / "private-output"
    output.mkdir(mode=0o700)
    descriptor = os.open(
        output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    child: subprocess.Popen[str] | None = None
    try:
        destination = f"/proc/self/fd/{descriptor}" if pinned else str(output)
        child = subprocess.Popen(
            [extractor, "x", "-y", f"-o{destination}", str(source)],
            close_fds=True,
            pass_fds=(descriptor,),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _pause_after_output_starts(child, output)
        original = tmp_path / "original-private-output"
        output.rename(original)
        output.mkdir(mode=0o700)
        (output / "unrelated.cbz").write_bytes(b"leave replacement alone")
        child.send_signal(signal.SIGCONT)
        stdout, stderr = child.communicate(timeout=15)
        assert not os.get_inheritable(descriptor), "parent FD must remain CLOEXEC"
        with zipfile.ZipFile(original / _FIRST) as archive:
            assert archive.getinfo("001.jpg").file_size == _PAGE_SIZE
            assert archive.testzip() is None
        later_root = original if pinned else output
        with zipfile.ZipFile(later_root / _LATER) as archive:
            assert archive.read("002.jpg") == b"later-page"
        assert (output / "unrelated.cbz").read_bytes() == b"leave replacement alone"
        if pinned:
            assert child.returncode == 0, f"{stdout}\n{stderr}"
            assert sorted(p.name for p in output.iterdir()) == ["unrelated.cbz"]
            assert sorted(
                str(p.relative_to(original)) for p in original.rglob("*.cbz")
            ) == [_LATER, _FIRST]
        else:
            assert not (original / _LATER).exists()
            # Metadata updates through the stale public pathname may fail;
            # the dangerous later write has already been observed regardless.
            assert child.returncode in (0, 1, 2), f"{stdout}\n{stderr}"
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        os.close(descriptor)
