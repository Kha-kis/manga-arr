"""Actual image codec/multipart/conversion controls; only private scratch writes."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import platform
import tempfile
import zipfile

import rarfile

from mangarr_archive_runtime_probe import PAGE, RAR, command


def probe() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (root / "tests/fixtures/archive_release/multipart-rar5.json").read_text()
    )
    payload = (
        b"".join(
            hashlib.sha256(f"mangarr-archive-{index}".encode()).digest()
            for index in range(512)
        )
        * 4
    )
    assert hashlib.sha256(payload).hexdigest() == manifest["payload_sha256"]
    assert len(payload) == manifest["payload_size"]
    rarfile.tool_setup(
        unrar=False,
        unar=False,
        bsdtar=False,
        sevenzip=True,
        sevenzip2=False,
        force=True,
    )
    result: dict[str, object] = {
        "python": platform.python_version(),
        "rarfile": rarfile.__version__,
    }
    with tempfile.TemporaryDirectory(prefix="archive-decoder-") as temporary:
        scratch = Path(temporary)
        for name, member in manifest["parts"].items():
            assert name in {f"multipart.part{index:02d}.rar" for index in range(1, 6)}
            data = base64.b64decode(member["base64"], validate=True)
            assert hashlib.sha256(data).hexdigest() == member["sha256"]
            (scratch / name).write_bytes(data)
        first = scratch / "multipart.part01.rar"
        with rarfile.RarFile(first) as archive:
            info = archive.getinfo("payload.bin")
            result["multipart_compressed"] = info.compress_type != rarfile.RAR_M0
            result["multipart_volumes"] = len(archive.volumelist())
            try:
                result["multipart_read_matches"] = (
                    archive.read("payload.bin") == payload
                )
            except rarfile.Error as error:
                result["multipart_error"] = str(error)[-4096:]
        output = scratch / "multipart-output"
        result["multipart_cli"] = command(["7z", "x", "-y", f"-o{output}", str(first)])
        extracted = output / "payload.bin"
        result["multipart_cli_matches"] = (
            extracted.is_file() and extracted.read_bytes() == payload
        )

        # Missing a real required volume must never be accepted as partial bytes.
        (scratch / "multipart.part05.rar").unlink()
        result["missing_volume_cli"] = command(["7z", "t", "-y", str(first)])
        try:
            with rarfile.RarFile(first) as archive:
                archive.read("payload.bin")
        except (rarfile.Error, FileNotFoundError) as error:
            result["missing_volume_rejected"] = type(error).__name__

        page = scratch / "001.png"
        page.write_bytes(PAGE)
        sevenzip = scratch / "control.7z"
        result["7z_create"] = command(["7z", "a", "-t7z", str(sevenzip), str(page)])
        sevenzip_output = scratch / "7z-output"
        result["7z_extract"] = command(
            ["7z", "x", "-y", f"-o{sevenzip_output}", str(sevenzip)]
        )
        result["7z_bytes_match"] = (sevenzip_output / "001.png").read_bytes() == PAGE
        cbz = scratch / "control.cbz"
        with zipfile.ZipFile(cbz, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("001.png", PAGE)
        cbz_output = scratch / "cbz-output"
        result["cbz_extract"] = command(["7z", "x", "-y", f"-o{cbz_output}", str(cbz)])
        result["cbz_bytes_match"] = (cbz_output / "001.png").read_bytes() == PAGE

        import files

        # Logging/database is external to codec-only conversion qualification.
        files.log_event = lambda *_args, **_kwargs: None
        cbr = scratch / "compressed.cbr"
        cbr.write_bytes(RAR)
        converted = files.convert_cbr_to_cbz(str(cbr))
        result["conversion_preserves_original"] = cbr.read_bytes() == RAR
        result["conversion_matches"] = False
        if converted is not None:
            with zipfile.ZipFile(converted) as archive:
                result["conversion_matches"] = (
                    archive.namelist() == ["001.png"]
                    and archive.read("001.png") == PAGE
                )
        copyright_path = Path("/usr/share/doc/7zip-rar/copyright")
        notice = copyright_path.read_text() if copyright_path.is_file() else ""
        normalized = " ".join(notice.split())
        result["plugin_license_retained"] = all(
            text in normalized
            for text in (
                "Copyright: Alexander L. Roshal",
                "License: unRAR",
                "cannot be used to re-create the RAR compression algorithm",
            )
        )
    return result


if __name__ == "__main__":
    print(json.dumps(probe(), sort_keys=True))
