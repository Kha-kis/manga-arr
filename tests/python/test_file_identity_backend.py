"""Disabled identity primitives; native ABI and media changes are simulated."""

from __future__ import annotations

import ctypes
import errno
import importlib
import json
import os
import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest


def _module() -> ModuleType:
    try:
        return importlib.import_module("file_identity")
    except ModuleNotFoundError:
        pytest.fail("bounded native identity primitives are not implemented")


class _ABIHandle(ctypes.Structure):
    _fields_ = [
        ("handle_bytes", ctypes.c_uint32),
        ("handle_type", ctypes.c_int32),
        ("f_handle", ctypes.c_ubyte * 128),
    ]


class _SimulatedNameToHandle:
    def __init__(
        self,
        payload: bytes = b"\x00\xff",
        handle_type: int = -7,
        error: int | None = None,
        reported_size: int | None = None,
    ) -> None:
        self.payload = payload
        self.handle_type = handle_type
        self.error = error
        self.reported_size = reported_size
        self.calls: list[tuple[int, bytes, int]] = []
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(
        self, fd: int, path: bytes, handle: Any, mount_id: Any, flags: int
    ) -> int:
        self.calls.append((fd, path, flags))
        buffer = ctypes.cast(handle, ctypes.POINTER(_ABIHandle)).contents
        assert buffer.handle_bytes == 128
        assert ctypes.sizeof(buffer) == 136
        buffer.handle_type = self.handle_type
        buffer.handle_bytes = (
            len(self.payload) if self.reported_size is None else self.reported_size
        )
        buffer.f_handle[: len(self.payload)] = self.payload
        ctypes.cast(mount_id, ctypes.POINTER(ctypes.c_int32)).contents.value = -123
        ctypes.set_errno(self.error or 0)
        return -1 if self.error is not None else 0


def _libc(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, call: _SimulatedNameToHandle
) -> list[str]:
    accessed: list[str] = []

    class Libc:
        def __getattr__(self, name: str) -> object:
            accessed.append(name)
            assert name == "name_to_handle_at", "unexpected libc/privilege operation"
            return call

    def load(name: str | None, *, use_errno: bool) -> Libc:
        assert name is None
        assert use_errno is True
        return Libc()

    monkeypatch.setattr(module.ctypes, "CDLL", load)
    return accessed


def test_capture_uses_borrowed_fd_empty_path_without_follow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module()
    call = _SimulatedNameToHandle()
    _libc(monkeypatch, module, call)
    path = tmp_path / "borrowed"
    path.write_bytes(b"fixture")
    with path.open("rb") as stream:
        fd = stream.fileno()
        result = module.capture_native(fd)
        assert os.fstat(fd).st_size == 7
        assert stream.read() == b"fixture"
    assert call.calls == [(fd, b"", 0x1000)]
    assert call.restype is ctypes.c_int
    assert len(call.argtypes) == 5
    assert call.argtypes[0] is ctypes.c_int
    assert call.argtypes[1] is ctypes.c_char_p
    assert call.argtypes[3] == ctypes.POINTER(ctypes.c_int32)
    assert call.argtypes[4] is ctypes.c_int
    assert result == module.NativeHandle(-7, b"\x00\xff")


@pytest.mark.parametrize("payload", [b"\x00", b"\x00\xff", bytes(range(128))])
@pytest.mark.parametrize("handle_type", [-(2**31), -1, 0, 2**31 - 1])
def test_capture_preserves_all_handle_bytes(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, handle_type: int
) -> None:
    module = _module()
    call = _SimulatedNameToHandle(payload, handle_type)
    _libc(monkeypatch, module, call)
    result = module.capture_native(42)
    assert result.handle_type == handle_type
    assert result.handle_bytes == payload
    assert module.NativeHandle.from_json(json.dumps(result.to_value())) == result


def test_no_handle_open_or_privilege_request(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    call = _SimulatedNameToHandle()
    accessed = _libc(monkeypatch, module, call)
    assert module.capture_native(42) == module.NativeHandle(-7, b"\x00\xff")
    assert accessed == ["name_to_handle_at"]
    assert len(call.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        errno.EPERM,
        errno.EACCES,
        errno.EINVAL,
        errno.EIO,
    ],
)
def test_simulated_native_errors_refuse_identity(
    monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    module = _module()
    call = _SimulatedNameToHandle(error=error)
    _libc(monkeypatch, module, call)
    with pytest.raises(module.IdentityUnavailable):
        module.capture_native(42)
    assert len(call.calls) == 1


@pytest.mark.parametrize("size", [0, 1, 127, 128, 129, 2**32 - 1])
def test_simulated_overflow_never_retries_or_returns_identity(
    monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    module = _module()
    call = _SimulatedNameToHandle(error=errno.EOVERFLOW, reported_size=size)
    _libc(monkeypatch, module, call)
    with pytest.raises(module.IdentityUnavailable):
        module.capture_native(42)
    assert len(call.calls) == 1


@pytest.mark.parametrize("size", [0, 129, 2**32 - 1])
def test_simulated_malformed_success_refuses_identity(
    monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    module = _module()
    _libc(monkeypatch, module, _SimulatedNameToHandle(reported_size=size))
    with pytest.raises(module.IdentityUnavailable):
        module.capture_native(42)


@pytest.mark.parametrize("missing_library", [False, True])
def test_simulated_missing_libc_symbol_refuses_identity(
    monkeypatch: pytest.MonkeyPatch, missing_library: bool
) -> None:
    module = _module()

    def load(*_args: object, **_kwargs: object) -> object:
        if missing_library:
            raise OSError("simulated libc unavailable")
        return SimpleNamespace()

    monkeypatch.setattr(module.ctypes, "CDLL", load)
    with pytest.raises(module.IdentityUnavailable):
        module.capture_native(42)


@pytest.mark.parametrize("fd", [True, False, -1, 2**31, 1.0, "1", None])
def test_invalid_fd_refuses_before_libc(
    monkeypatch: pytest.MonkeyPatch, fd: object
) -> None:
    module = _module()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid FD reached libc")

    monkeypatch.setattr(module.ctypes, "CDLL", forbidden)
    with pytest.raises(module.InvalidIdentity):
        module.capture_native(fd)


def test_native_wire_round_trip_and_frozen_value() -> None:
    module = _module()
    handle = module.NativeHandle(1, b"\x00\xff")
    assert handle.to_value() == {
        "version": 1,
        "backend": "linux-exportfs",
        "handle_type": 1,
        "handle_size": 2,
        "handle_hex": "00ff",
    }
    assert module.NativeHandle.from_value(handle.to_value()) == handle
    with pytest.raises(FrozenInstanceError):
        handle.handle_type = 2


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("version", True),
        ("version", 1.0),
        ("version", 0),
        ("version", 2),
        ("backend", "native"),
        ("backend", None),
        ("handle_type", True),
        ("handle_type", 1.0),
        ("handle_type", "1"),
        ("handle_type", -(2**31) - 1),
        ("handle_type", 2**31),
        ("handle_size", True),
        ("handle_size", 2.0),
        ("handle_size", 0),
        ("handle_size", 1),
        ("handle_size", 129),
        ("handle_hex", ""),
        ("handle_hex", "0"),
        ("handle_hex", "00FF"),
        ("handle_hex", "00 ff"),
        ("handle_hex", "00fg"),
        ("handle_hex", "00" * 129),
        ("handle_hex", None),
    ],
)
def test_strict_native_json_rejects_aliases(field: str, invalid: object) -> None:
    module = _module()
    value = module.NativeHandle(1, b"\x00\xff").to_value()
    value[field] = invalid
    with pytest.raises(module.InvalidIdentity):
        module.NativeHandle.from_value(value)
    with pytest.raises(module.InvalidIdentity):
        module.NativeHandle.from_json(json.dumps(value))


@pytest.mark.parametrize(
    "encoded",
    [
        '{"version":1,"version":1,"backend":"linux-exportfs","handle_type":1,"handle_size":2,"handle_hex":"00ff"}',
        '{"version":1,"backend":"linux-exportfs","handle_type":1,"handle_size":2,"handle_hex":"00ff","extra":null}',
        '{"version":1,"backend":"linux-exportfs","handle_type":1,"handle_size":2}',
        "[]",
        "null",
        "{",
        "NaN",
        "Infinity",
    ],
)
def test_strict_native_json_rejects_duplicate_unknown_missing_and_nonobjects(
    encoded: str,
) -> None:
    module = _module()
    with pytest.raises(module.InvalidIdentity):
        module.NativeHandle.from_json(encoded)


@pytest.mark.parametrize(
    ("handle_type", "payload"),
    [
        (True, b"a"),
        (1.0, b"a"),
        (2**31, b"a"),
        (1, b""),
        (1, b"a" * 129),
        (1, bytearray(b"a")),
        (1, "a"),
    ],
)
def test_native_direct_construction_cannot_bypass_validation(
    handle_type: object, payload: object
) -> None:
    module = _module()
    with pytest.raises(module.InvalidIdentity):
        module.NativeHandle(handle_type, payload)


@pytest.mark.parametrize(
    "domain",
    [
        "namespace",
        "rescan",
        "publication",
        "deletion",
        "pack_reservation",
        "pack_tombstone",
        "queue_source",
    ],
)
def test_owner_ref_preserves_canonical_tuple(domain: str) -> None:
    module = _module()
    assert module.OwnerRef(domain, ("opaque-key", 7)).key == ("opaque-key", 7)


@pytest.mark.parametrize(
    ("domain", "key"),
    [
        ("other", (1,)),
        ("", (1,)),
        ("deletion", ()),
        ("deletion", [1]),
        ("deletion", (True,)),
        ("deletion", (1.0,)),
        ("deletion", ("",)),
        ("deletion", ("bad\x00key",)),
    ],
)
def test_owner_ref_rejects_invalid_direct_construction(
    domain: object, key: object
) -> None:
    module = _module()
    with pytest.raises(module.InvalidIdentity):
        module.OwnerRef(domain, key)


@pytest.mark.parametrize("kind", ["file", "directory", "marker"])
def test_role_key_preserves_safe_relative_and_optional_fields(kind: str) -> None:
    module = _module()
    assert (
        module.RoleKey(7, "cleanup", kind, "folder/chapter.cbz").relative
        == "folder/chapter.cbz"
    )
    assert module.RoleKey(None, "cleanup", kind, None).file_id is None


@pytest.mark.parametrize(
    "relative", ["", "/x", "..", "a/../b", "./a", "a//b", "a/", "a\x00b", "a\\..\\b"]
)
def test_role_key_rejects_unsafe_relative_without_normalizing(relative: str) -> None:
    module = _module()
    with pytest.raises(module.InvalidIdentity):
        module.RoleKey(7, "cleanup", "file", relative)


@pytest.mark.parametrize(
    ("file_id", "purpose", "kind"),
    [
        (True, "cleanup", "file"),
        (-1, "cleanup", "file"),
        (1.0, "cleanup", "file"),
        (7, "", "file"),
        (7, "\x00", "file"),
        (7, "cleanup", "symlink"),
    ],
)
def test_role_key_rejects_invalid_identifiers(
    file_id: object, purpose: object, kind: object
) -> None:
    module = _module()
    with pytest.raises(module.InvalidIdentity):
        module.RoleKey(file_id, purpose, kind, None)


@pytest.mark.parametrize("purpose", [" ", "\t", "\n"])
def test_role_key_rejects_blank_purpose_without_normalizing(purpose: str) -> None:
    module = _module()
    with pytest.raises(module.InvalidIdentity):
        module.RoleKey(7, purpose, "file", None)


def _candidate_contracts() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "platforms": [
                {
                    "arch": "simulated-arch",
                    "kernel_release": "simulated-kernel",
                    "libc_version": "simulated-libc",
                    "encoder_revision": "simulated-encoder",
                }
            ],
            "mount": {
                "filesystem": "nfs4",
                "nfs_version": "4.1",
                "transport": "tcp",
                "security": "sys",
                "hard": True,
            },
        },
        {
            "vendor": "simulated-vendor",
            "version": "simulated-version",
            "filesystem": "simulated-fs",
            "exporter": "simulated-exporter",
            "handle_contract": "simulated-opaque-handles",
            "allowed_transitions": ["simulated-remount"],
            "recovery_transitions": ["simulated-recovery"],
        },
    )


def test_simulated_candidate_cannot_self_qualify_or_mutate_qualification() -> None:
    module = _module()
    client, server = _candidate_contracts()
    profile = module.StorageProfile("simulated", 1, "0" * 64, client, server)
    with pytest.raises(module.IdentityUnavailable):
        module.validate_profile(profile)
    client.clear()
    server["vendor"] = "changed"
    assert profile.client_contract == _candidate_contracts()[0]
    assert profile.server_contract == _candidate_contracts()[1]
    profile.client_contract.clear()
    profile.server_contract["vendor"] = "another change"
    assert profile.client_contract == _candidate_contracts()[0]
    assert profile.server_contract == _candidate_contracts()[1]
    with pytest.raises(module.IdentityUnavailable):
        module.validate_profile(profile)
    with pytest.raises(FrozenInstanceError):
        profile.revision = 2


@pytest.mark.parametrize(
    ("profile_id", "revision", "digest"),
    [
        ("", 1, "0" * 64),
        ("bad\x00id", 1, "0" * 64),
        ("simulated", True, "0" * 64),
        ("simulated", 0, "0" * 64),
        ("simulated", 1.0, "0" * 64),
        ("simulated", 1, "A" * 64),
        ("simulated", 1, "0" * 63),
    ],
)
def test_profile_rejects_invalid_direct_identifiers(
    profile_id: object, revision: object, digest: object
) -> None:
    module = _module()
    client, server = _candidate_contracts()
    with pytest.raises(module.InvalidIdentity):
        module.StorageProfile(profile_id, revision, digest, client, server)


@pytest.mark.parametrize("side", ["client", "server", "platform", "mount"])
@pytest.mark.parametrize("operation", ["extra", "missing"])
def test_profile_rejects_unknown_and_missing_contract_keys(
    side: str, operation: str
) -> None:
    module = _module()
    client, server = _candidate_contracts()
    containers: dict[str, Any] = {
        "client": client,
        "server": server,
        "platform": cast(list[object], client["platforms"])[0],
        "mount": client["mount"],
    }
    container = containers[side]
    if operation == "extra":
        container["unqualified_extra"] = True
    else:
        container.pop(next(iter(container)))
    with pytest.raises(module.InvalidIdentity):
        module.StorageProfile("simulated", 1, "0" * 64, client, server)


@pytest.mark.parametrize(
    "invalid", [float("nan"), float("inf"), -float("inf"), object(), {1: "bad-key"}]
)
def test_profile_rejects_nonfinite_or_nonjson_contract_values(invalid: object) -> None:
    module = _module()
    client, server = _candidate_contracts()
    server["handle_contract"] = invalid
    with pytest.raises(module.InvalidIdentity):
        module.StorageProfile("simulated", 1, "0" * 64, client, server)


def test_profile_validation_rejects_nonprofile_and_cyclic_contracts() -> None:
    module = _module()
    with pytest.raises(module.InvalidIdentity):
        module.validate_profile({"profile_id": "simulated"})
    client, server = _candidate_contracts()
    server["handle_contract"] = server
    with pytest.raises(module.InvalidIdentity):
        module.StorageProfile("simulated", 1, "0" * 64, client, server)


def test_simulated_helper_distinguishes_copy_replacement_alias_and_rename(
    tmp_path: Path,
) -> None:
    try:
        helpers = importlib.import_module("file_identity_helpers")
    except ModuleNotFoundError:
        pytest.fail("simulated identity helper is not implemented")
    source = tmp_path / "source"
    source.write_bytes(b"same content")
    copied = tmp_path / "copy"
    copied.write_bytes(source.read_bytes())
    alias = tmp_path / "alias"
    os.link(source, alias)
    with helpers.SimulatedIdentityBackend() as backend:
        original = backend.capture(source)
        assert backend.capture(alias) == original
        assert backend.capture(copied) != original
        renamed = tmp_path / "renamed"
        source.rename(renamed)
        assert backend.capture(renamed) == original
        source.write_bytes(b"same content")
        assert backend.capture(source) != original


def test_simulated_media_device_proxy_never_changes_local_guard_or_sqlite(
    tmp_path: Path,
) -> None:
    try:
        helpers = importlib.import_module("file_identity_helpers")
    except ModuleNotFoundError:
        pytest.fail("simulated media proxy is not implemented")
    import file_mutation_lock as locks

    media = tmp_path / "media"
    media.mkdir()
    book = media / "book"
    book.write_bytes(b"fixture")
    config = tmp_path / "config"
    config.mkdir()
    db_path = config / "fixture.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE fixture (value TEXT)")
    before = (os.stat(db_path), os.stat(config))
    consumer = SimpleNamespace(os=helpers.MediaDeviceProxy(media, device=999999))
    assert consumer.os.stat(book).st_dev == 999999
    assert consumer.os.stat(book).st_ino == os.stat(book).st_ino
    assert consumer.os.stat(db_path).st_dev == before[0].st_dev
    assert consumer.os.stat(config).st_dev == before[1].st_dev
    assert locks.os is os
    with locks.file_mutation_guard(str(db_path)) as guard:
        guard.verify()
        assert os.fstat(guard.subprocess_fd).st_dev == before[1].st_dev
        with sqlite3.connect(db_path) as db:
            db.execute("INSERT INTO fixture VALUES ('safe')")
        guard.verify()
    assert (os.stat(db_path).st_dev, os.stat(db_path).st_ino) == (
        before[0].st_dev,
        before[0].st_ino,
    )


def test_simulated_media_proxy_excludes_path_escape_and_symlink_targets(
    tmp_path: Path,
) -> None:
    helpers = importlib.import_module("file_identity_helpers")
    media = tmp_path / "media"
    media.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    database = config / "fixture.db"
    database.write_bytes(b"local fixture")
    (media / "config-link").symlink_to(config, target_is_directory=True)
    proxy = helpers.MediaDeviceProxy(media, device=999999)
    for path in (
        media / ".." / "config" / "fixture.db",
        media / "config-link" / "fixture.db",
    ):
        assert proxy.stat(path).st_dev == os.stat(database).st_dev
