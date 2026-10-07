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


@pytest.mark.parametrize("through_symlink", [False, True])
def test_inject_streams_entries_without_whole_archive_allocation(
    tmp_path: Path,
    through_symlink: bool,
) -> None:
    import comicinfo

    cbz_path = tmp_path / "large.cbz"
    page = b"x" * (24 * 1024 * 1024)
    _make_cbz(cbz_path, [("001.bin", page), ("002.bin", page)])
    injection_path = cbz_path
    if through_symlink:
        injection_path = tmp_path / "large-alias.cbz"
        injection_path.symlink_to(cbz_path.name)
    del page
    gc.collect()

    tracemalloc.start()
    try:
        assert comicinfo.inject_comicinfo(str(injection_path), _XML) is True
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 8 * 1024 * 1024, f"peak allocation was {peak / 2**20:.1f} MiB"
    with zipfile.ZipFile(cbz_path) as archive:
        assert archive.read("ComicInfo.xml") == _XML.encode("utf-8")


def _make_symlink_alias(
    path: Path,
    target: Path,
    kind: Literal["relative", "absolute", "chained"],
) -> list[Path]:
    if kind == "absolute":
        path.symlink_to(target)
        return [path]
    if kind == "chained":
        intermediate = path.parent / "intermediate.cbz"
        intermediate.symlink_to(os.path.relpath(target, intermediate.parent))
        path.symlink_to(intermediate.name)
        return [path, intermediate]
    path.symlink_to(os.path.relpath(target, path.parent))
    return [path]


@pytest.mark.parametrize("kind", ["relative", "absolute", "chained"])
def test_inject_preserves_symlinks_and_updates_target_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["relative", "absolute", "chained"],
) -> None:
    import comicinfo

    target_directory = tmp_path / "target"
    alias_directory = tmp_path / "aliases"
    target_directory.mkdir()
    alias_directory.mkdir()
    target = target_directory / "original.cbz"
    _make_cbz(target, [("001.png", b"page")])
    target.chmod(0o640)
    alias = alias_directory / "injection.cbz"
    links = _make_symlink_alias(alias, target, kind)
    other_alias = alias_directory / "other.cbz"
    other_alias.symlink_to(target)
    links.append(other_alias)
    original_links = [(link, link.readlink(), link.lstat().st_ino) for link in links]
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def replace_sibling(source: str, destination: str) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(comicinfo.os, "replace", replace_sibling)

    assert comicinfo.inject_comicinfo(str(alias), _XML) is True

    assert len(replacements) == 1
    temporary_path, replacement_path = replacements[0]
    assert temporary_path.parent == target_directory
    assert replacement_path == target
    for link, original_destination, original_inode in original_links:
        assert link.is_symlink()
        assert link.readlink() == original_destination
        assert link.lstat().st_ino == original_inode
        assert _archive_entries(link) == [
            ("ComicInfo.xml", _XML.encode("utf-8")),
            ("001.png", b"page"),
        ]
    assert _archive_entries(target) == _archive_entries(alias)
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert _temporary_archives(target_directory) == []
    assert _temporary_archives(alias_directory) == []


@pytest.mark.parametrize("kind", ["relative", "absolute", "chained"])
@pytest.mark.parametrize("failure", ["copy", "fsync", "replace"])
def test_symlink_rewrite_failure_preserves_target_and_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["relative", "absolute", "chained"],
    failure: Literal["copy", "fsync", "replace"],
) -> None:
    import comicinfo

    target_directory = tmp_path / "target"
    alias_directory = tmp_path / "aliases"
    target_directory.mkdir()
    alias_directory.mkdir()
    target = target_directory / "original.cbz"
    _make_cbz(target, [("001.png", b"page")])
    original = target.read_bytes()
    original_inode = target.stat().st_ino
    alias = alias_directory / "injection.cbz"
    links = _make_symlink_alias(alias, target, kind)
    original_links = [(link, link.readlink(), link.lstat().st_ino) for link in links]

    def fail_operation(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"simulated {failure} failure")

    if failure == "copy":
        monkeypatch.setattr(shutil, "copyfileobj", fail_operation)
    else:
        monkeypatch.setattr(comicinfo.os, failure, fail_operation)
    monkeypatch.setattr(comicinfo, "log_event", lambda *args, **kwargs: None)

    assert comicinfo.inject_comicinfo(str(alias), _XML) is False

    assert target.read_bytes() == original
    assert target.stat().st_ino == original_inode
    for link, original_destination, link_inode in original_links:
        assert link.is_symlink()
        assert link.readlink() == original_destination
        assert link.lstat().st_ino == link_inode
        assert link.read_bytes() == original
    assert _temporary_archives(target_directory) == []
    assert _temporary_archives(alias_directory) == []


@pytest.mark.parametrize("kind", ["directory", "missing", "loop"])
def test_invalid_symlink_target_is_not_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["directory", "missing", "loop"],
) -> None:
    import comicinfo

    alias = tmp_path / "invalid.cbz"
    target = tmp_path / "target"
    if kind == "directory":
        target.mkdir()
        _make_cbz(target / "inside.cbz", [("001.png", b"page")])
    alias.symlink_to(alias.name if kind == "loop" else target.name)
    link_destination = alias.readlink()
    link_inode = alias.lstat().st_ino
    monkeypatch.setattr(comicinfo, "log_event", lambda *args, **kwargs: None)

    assert comicinfo.inject_comicinfo(str(alias), _XML) is False

    assert alias.is_symlink()
    assert alias.readlink() == link_destination
    assert alias.lstat().st_ino == link_inode
    if kind == "directory":
        assert _archive_entries(target / "inside.cbz") == [("001.png", b"page")]
        assert _temporary_archives(target) == []
    else:
        assert not target.exists()
    assert _temporary_archives(tmp_path) == []


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


@pytest.mark.parametrize("through_symlink", [False, True])
def test_injecting_hardlink_destination_does_not_mutate_source_alias(
    tmp_path: Path,
    through_symlink: bool,
) -> None:
    import comicinfo

    source_path = tmp_path / "source.cbz"
    destination_path = tmp_path / "manual-import.cbz"
    _make_cbz(source_path, [("001.png", b"source page")])
    original = source_path.read_bytes()
    source_inode = source_path.stat().st_ino
    os.link(source_path, destination_path)
    injection_path = destination_path
    if through_symlink:
        injection_path = tmp_path / "manual-import-alias.cbz"
        injection_path.symlink_to(destination_path.name)

    assert comicinfo.inject_comicinfo(str(injection_path), _XML) is True

    assert source_path.read_bytes() == original
    assert source_path.stat().st_ino == source_inode
    assert destination_path.stat().st_ino != source_inode
    if through_symlink:
        assert injection_path.is_symlink()
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
