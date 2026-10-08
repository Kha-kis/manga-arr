"""Bounded current-image archive qualification; no product or host mutation."""

from __future__ import annotations

import base64
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

import rarfile

# Verbatim synthetic RAR 7.11 asset from Pasteur's standalone acceptance test.
RAR = base64.b64decode(
    "UmFyIRoHAQAzkrXlCgEFBgAFAQGAgAD9y50EJQIDC4UBBJwBtIMCM4Xr04AFAQcwMDEucG5n"
    "CgMTgfnGamccjgDFHYI2ZmQj+CP+0TYWcitqqm4BZKP4JxA5JCASEEkJDAhYkSIJOwcQCBpC"
    "QwEWLAhgQoWJFGB8YKYd9PvfWe5P5gXmRGZYWEj7RAAEVcjR2Pn+O5z986mOoSkTcjn2xTFd"
    "8C4Y0JrJtzaRPZt5YSY8W1IPpUdLSX/uzvqnj+eQKqwStSCUHXdWUQMFBAA="
)
PAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAY0lEQVR4nO3PQQ3AIADAQEADhrCG8ongcVnSU9DOfc/4s6UDXjWgNaA1oDWgNaA1oDWgNaA1oDWgNaA1oDWgNaA1oDWgNaA1oDWgNaA1oDWgNaA1oDWgNaA1oDWgNaA1oDWgfccxAVcRofg0AAAAAElFTkSuQmCC"
)
RAR_SHA256 = "96cd790a41d788e95597fc25a236c81c698c9ca4a95e2a99c3b5dd0edf66f9db"


def command(arguments: list[str]) -> dict[str, object]:
    completed = subprocess.run(
        arguments, capture_output=True, text=True, timeout=10, check=False
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4096:],
        "stderr": completed.stderr[-4096:],
    }


def probe() -> dict[str, object]:
    assert hashlib.sha256(RAR).hexdigest() == RAR_SHA256
    extractor = shutil.which("7z")
    assert extractor is not None
    result: dict[str, object] = {
        "rarfile": version("rarfile"),
        "fixture_sha256": RAR_SHA256,
        "fixture_size": len(RAR),
        "expected_page_sha256": hashlib.sha256(PAGE).hexdigest(),
        "packages": command(
            [
                "dpkg-query",
                "-W",
                "-f",
                "${binary:Package} ${Status} ${Version}\n",
                "7zip",
                "7zip-rar",
            ]
        ),
        "rar_codec_present": Path("/usr/lib/7zip/Codecs/Rar.so").is_file(),
        "expected_page_size": len(PAGE),
        "apt_components": [
            line
            for line in Path("/etc/apt/sources.list.d/debian.sources")
            .read_text()
            .splitlines()
            if line.startswith("Components:")
        ],
    }
    with tempfile.TemporaryDirectory(prefix="archive-probe-") as temporary:
        root = Path(temporary)
        source = root / "compressed.cbr"
        source.write_bytes(RAR)
        result["rar_headers"] = command([extractor, "l", "-slt", str(source)])
        output = root / "rar-output"
        result["rar_extract"] = command(
            [extractor, "x", "-y", f"-o{output}", str(source)]
        )
        extracted = output / "001.png"
        result["rar_bytes_match"] = (
            extracted.is_file() and extracted.read_bytes() == PAGE
        )
        with rarfile.RarFile(source) as archive:
            info = archive.getinfo("001.png")
            result["rarfile_header"] = {
                "file_size": info.file_size,
                "compress_size": info.compress_size,
                "compress_type": info.compress_type,
                "stored_type": rarfile.RAR_M0,
            }
            try:
                rarfile.tool_setup(
                    unrar=False,
                    unar=False,
                    bsdtar=False,
                    sevenzip=True,
                    sevenzip2=False,
                    force=True,
                )
                result["rarfile_bytes_match"] = archive.read("001.png") == PAGE
            except rarfile.Error as error:
                result["rarfile_read_error"] = {
                    "type": type(error).__name__,
                    "message": str(error)[-4096:],
                }
        zip_source = root / "control.zip"
        with zipfile.ZipFile(zip_source, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("001.png", PAGE)
        zip_output = root / "zip-output"
        result["zip_extract"] = command(
            [extractor, "x", "-y", f"-o{zip_output}", str(zip_source)]
        )
        result["zip_bytes_match"] = (zip_output / "001.png").read_bytes() == PAGE
    return result


if __name__ == "__main__":
    print(json.dumps(probe(), sort_keys=True))
