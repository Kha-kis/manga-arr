"""Regression coverage for bounded, failure-atomic ComicInfo rewrites."""

from __future__ import annotations

import errno
import gc
import os
import shutil
import stat
import tempfile
import tracemalloc
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

import conftest  # noqa: F401, E402


_XML = "<ComicInfo><Series>Replacement</Series></ComicInfo>"


def _make_cbz(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for name, content in entries:
            archive.writestr(name, content)


def _archive_entries(path: Path) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        return [(info.filename, archive.read(info)) for info in archive.infolist()]


def _temporary_archives(directory: Path) -> list[Path]:
    return list(directory.glob(".comicinfo-*"))


def test_inject_streams_entries_without_whole_archive_allocation(
    tmp_path: Path,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "large.cbz"
    page = b"x" * (24 * 1024 * 1024)
    _make_cbz(cbz_path, [("001.bin", page), ("002.bin", page)])
    del page
    gc.collect()

    tracemalloc.start()
    try:
        assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is True
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 8 * 1024 * 1024, f"peak allocation was {peak / 2**20:.1f} MiB"


def test_rewrite_failure_leaves_original_archive_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "failure.cbz"
    _make_cbz(cbz_path, [("001.bin", b"first"), ("002.bin", b"second")])
    original = cbz_path.read_bytes()
    real_open = zipfile.ZipFile.open
    write_count = 0

    def fail_second_output_entry(
        self,
        name,
        mode: Literal["r", "w"] = "r",
        pwd=None,
        *,
        force_zip64=False,
    ):
        nonlocal write_count
        if mode == "w":
            write_count += 1
            if write_count == 2:
                raise OSError("simulated archive write failure")
        return real_open(
            self,
            name,
            mode=mode,
            pwd=pwd,
            force_zip64=force_zip64,
        )

    monkeypatch.setattr(zipfile.ZipFile, "open", fail_second_output_entry)
    monkeypatch.setattr(comicinfo, "log_event", lambda *args, **kwargs: None)

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is False
    assert cbz_path.read_bytes() == original
    assert _temporary_archives(tmp_path) == []


def test_success_preserves_entry_order_content_and_stored_compression(
    tmp_path: Path,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "success.cbz"
    _make_cbz(
        cbz_path,
        [
            ("ComicInfo.xml", b"old root metadata"),
            ("chapter/", b""),
            ("chapter/001.png", b"page one"),
            ("nested/comicinfo.XML", b"old nested metadata"),
            ("002.png", b"page two"),
        ],
    )

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is True

    assert _archive_entries(cbz_path) == [
        ("ComicInfo.xml", _XML.encode("utf-8")),
        ("chapter/", b""),
        ("chapter/001.png", b"page one"),
        ("002.png", b"page two"),
    ]
    with zipfile.ZipFile(cbz_path) as archive:
        assert archive.testzip() is None
        assert archive.getinfo("chapter/").is_dir()
        assert all(
            info.compress_type == zipfile.ZIP_STORED for info in archive.infolist()
        )
    assert _temporary_archives(tmp_path) == []


def test_nonempty_directory_entry_preserves_content_and_metadata(
    tmp_path: Path,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "directory-payload.cbz"
    directory_info = zipfile.ZipInfo("unusual/", date_time=(2021, 2, 3, 4, 5, 6))
    directory_info.compress_type = zipfile.ZIP_STORED
    directory_info.external_attr = (0o40750 << 16) | 0x10
    directory_info.internal_attr = 1
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(directory_info, b"nonempty directory payload")

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is True

    with zipfile.ZipFile(cbz_path) as archive:
        rewritten_info = archive.getinfo("unusual/")
        assert archive.read(rewritten_info) == b"nonempty directory payload"
        assert rewritten_info.date_time == directory_info.date_time
        assert rewritten_info.external_attr == directory_info.external_attr
        assert rewritten_info.internal_attr == directory_info.internal_attr


def test_corrupt_directory_entry_fails_without_replacing_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "corrupt-directory-payload.cbz"
    payload = b"unique directory payload"
    _make_cbz(cbz_path, [("unusual/", payload), ("001.png", b"page")])
    damaged = bytearray(cbz_path.read_bytes())
    damaged[damaged.index(payload)] ^= 0xFF
    cbz_path.write_bytes(damaged)
    original = cbz_path.read_bytes()
    monkeypatch.setattr(comicinfo, "log_event", lambda *args, **kwargs: None)

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is False
    assert cbz_path.read_bytes() == original
    assert _temporary_archives(tmp_path) == []


def test_compressed_source_entry_is_rewritten_intact_as_stored(tmp_path: Path) -> None:
    import comicinfo

    cbz_path = tmp_path / "compressed.cbz"
    content = (b"compressed source page content\n" * 4096) + bytes(range(256))
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("001.bin", content)
    with zipfile.ZipFile(cbz_path) as archive:
        assert archive.getinfo("001.bin").compress_type == zipfile.ZIP_DEFLATED

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is True

    with zipfile.ZipFile(cbz_path) as archive:
        assert archive.read("001.bin") == content
        assert archive.getinfo("001.bin").compress_type == zipfile.ZIP_STORED
        assert archive.testzip() is None


def test_success_preserves_archive_file_mode(tmp_path: Path) -> None:
    import comicinfo

    cbz_path = tmp_path / "mode.cbz"
    _make_cbz(cbz_path, [("001.png", b"page")])
    os.chmod(cbz_path, 0o640)

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is True

    assert stat.S_IMODE(cbz_path.stat().st_mode) == 0o640


def test_foreign_owner_permission_error_retries_group_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "shared-group.cbz"
    _make_cbz(cbz_path, [("001.png", b"page")])
    os.chmod(cbz_path, 0o660)
    real_stat = os.stat
    archive_stat = real_stat(cbz_path)
    foreign_uid = archive_stat.st_uid + 1
    original_gid = archive_stat.st_gid + 27
    calls: list[tuple[int, int]] = []

    def foreign_owner_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.path.abspath(os.fspath(path)) == os.path.abspath(cbz_path):
            return SimpleNamespace(
                st_uid=foreign_uid,
                st_gid=original_gid,
                st_mode=result.st_mode,
            )
        return result

    def record_chown(descriptor: int, uid: int, gid: int) -> None:
        calls.append((uid, gid))
        if uid == foreign_uid:
            raise PermissionError(errno.EPERM, "simulated foreign-owner fchown")

    monkeypatch.setattr(comicinfo.os, "stat", foreign_owner_stat)
    monkeypatch.setattr(comicinfo.os, "fchown", record_chown, raising=False)

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is True
    assert calls == [(foreign_uid, original_gid), (-1, original_gid)]
    assert stat.S_IMODE(cbz_path.stat().st_mode) == 0o660
    assert _archive_entries(cbz_path) == [
        ("ComicInfo.xml", _XML.encode("utf-8")),
        ("001.png", b"page"),
    ]
    assert _temporary_archives(tmp_path) == []


def test_injecting_hardlink_destination_does_not_mutate_source_alias(
    tmp_path: Path,
) -> None:
    import comicinfo

    source_path = tmp_path / "source.cbz"
    destination_path = tmp_path / "manual-import.cbz"
    _make_cbz(source_path, [("001.png", b"source page")])
    original = source_path.read_bytes()
    source_inode = source_path.stat().st_ino
    os.link(source_path, destination_path)

    assert comicinfo.inject_comicinfo(str(destination_path), _XML) is True

    assert source_path.read_bytes() == original
    assert source_path.stat().st_ino == source_inode
    assert destination_path.stat().st_ino != source_inode
    assert _archive_entries(destination_path)[0] == (
        "ComicInfo.xml",
        _XML.encode("utf-8"),
    )


def test_streamed_entry_uses_zip64_when_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "zip64.cbz"
    _make_cbz(cbz_path, [("001.bin", b"x" * 512)])
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 128)

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is True

    with zipfile.ZipFile(cbz_path) as archive:
        info = archive.getinfo("001.bin")
        assert archive.read(info) == b"x" * 512
        assert info.extract_version >= zipfile.ZIP64_VERSION


def test_read_failure_removes_temporary_archive_and_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "read-failure.cbz"
    _make_cbz(cbz_path, [("001.bin", b"first"), ("002.bin", b"second")])
    original = cbz_path.read_bytes()
    real_read = zipfile.ZipExtFile.read

    def fail_selected_entry(self, size=-1):
        if getattr(self, "name", None) == "002.bin":
            raise OSError("simulated archive read failure")
        return real_read(self, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", fail_selected_entry)
    monkeypatch.setattr(comicinfo, "log_event", lambda *args, **kwargs: None)

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is False
    assert cbz_path.read_bytes() == original
    assert _temporary_archives(tmp_path) == []


def test_crc_failure_removes_temporary_archive_and_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "crc-failure.cbz"
    payload = b"unique stored payload"
    _make_cbz(cbz_path, [("001.bin", payload)])
    damaged = bytearray(cbz_path.read_bytes())
    payload_offset = damaged.index(payload)
    damaged[payload_offset] ^= 0xFF
    cbz_path.write_bytes(damaged)
    original = cbz_path.read_bytes()
    monkeypatch.setattr(comicinfo, "log_event", lambda *args, **kwargs: None)

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is False
    assert cbz_path.read_bytes() == original
    assert _temporary_archives(tmp_path) == []


def test_copy_failure_removes_temporary_archive_and_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "copy-failure.cbz"
    _make_cbz(cbz_path, [("001.bin", b"page")])
    original = cbz_path.read_bytes()

    def fail_copy(source, destination, length=0) -> None:
        raise OSError("simulated copy failure")

    monkeypatch.setattr(shutil, "copyfileobj", fail_copy)
    monkeypatch.setattr(comicinfo, "log_event", lambda *args, **kwargs: None)

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is False
    assert cbz_path.read_bytes() == original
    assert _temporary_archives(tmp_path) == []


def test_chmod_failure_removes_temporary_archive_and_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "chmod-failure.cbz"
    _make_cbz(cbz_path, [("001.bin", b"page")])
    original = cbz_path.read_bytes()

    def fail_chmod(descriptor: int, mode: int) -> None:
        raise OSError("simulated chmod failure")

    monkeypatch.setattr(comicinfo.os, "fchmod", fail_chmod, raising=False)
    monkeypatch.setattr(comicinfo, "log_event", lambda *args, **kwargs: None)

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is False
    assert cbz_path.read_bytes() == original
    assert _temporary_archives(tmp_path) == []


def test_replace_failure_removes_temporary_archive_and_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "replace-failure.cbz"
    _make_cbz(cbz_path, [("001.bin", b"page")])
    original = cbz_path.read_bytes()

    def fail_replace(source: str, destination: str) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(comicinfo.os, "replace", fail_replace, raising=False)
    monkeypatch.setattr(comicinfo, "log_event", lambda *args, **kwargs: None)

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is False
    assert cbz_path.read_bytes() == original
    assert _temporary_archives(tmp_path) == []


def test_success_closes_temporary_file_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "descriptor.cbz"
    _make_cbz(cbz_path, [("001.bin", b"page")])
    created_descriptor: int | None = None
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        nonlocal created_descriptor
        created_descriptor, path = real_mkstemp(*args, **kwargs)
        return created_descriptor, path

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)

    assert comicinfo.inject_comicinfo(str(cbz_path), _XML) is True
    assert created_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(created_descriptor)
