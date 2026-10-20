"""Bounded native identity primitives, not enabled for application ownership.

An exportfs handle is opaque evidence, not permission to mutate a file. No
storage profile is qualified here, and no application caller uses this module.
"""

from __future__ import annotations

import ctypes
import json
import math
from dataclasses import dataclass, field
from typing import cast

_MAX_HANDLE_BYTES = 128
_AT_EMPTY_PATH = 0x1000
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_HEX = frozenset("0123456789abcdef")
_OWNER_DOMAINS = frozenset(
    {
        "namespace",
        "rescan",
        "publication",
        "deletion",
        "pack_reservation",
        "pack_tombstone",
        "queue_source",
    }
)


class FileIdentityError(RuntimeError):
    """Identity evidence does not authorize the requested operation."""


class IdentityUnavailable(FileIdentityError):
    """The bounded native backend cannot supply usable evidence."""


class IdentityMismatch(FileIdentityError):
    """Identity evidence differs from the expected object or contract."""


class InvalidIdentity(FileIdentityError):
    """An identity value is malformed or noncanonical."""


def _integer(value: object, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int:
        raise InvalidIdentity("identity integer has an invalid type")
    number = cast(int, value)
    if number < minimum or (maximum is not None and number > maximum):
        raise InvalidIdentity("identity integer is out of range")
    return number


def _text(value: object) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise InvalidIdentity("invalid identity string")
    return cast(str, value)


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise InvalidIdentity("invalid identity fields")
    return cast(dict[str, object], value)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidIdentity("duplicate identity JSON key")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise InvalidIdentity("nonfinite identity JSON constant")


def _json_value(value: object, *, depth: int = 0) -> None:
    if depth > 32:
        raise InvalidIdentity("identity contract nesting is excessive or cyclic")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(cast(float, value)):
        return
    if type(value) is list:
        for child in cast(list[object], value):
            _json_value(child, depth=depth + 1)
        return
    if type(value) is dict:
        for key, child in cast(dict[object, object], value).items():
            _text(key)
            _json_value(child, depth=depth + 1)
        return
    raise InvalidIdentity("identity contracts require finite JSON values")


def _contract_json(value: dict[str, object]) -> str:
    _json_value(value)
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True, init=False)
class StorageProfile:
    """An immutable candidate snapshot, never a source qualification record."""

    profile_id: str
    revision: int
    qualification_sha256: str
    _client_json: str = field(repr=False)
    _server_json: str = field(repr=False)

    def __init__(
        self,
        profile_id: str,
        revision: int,
        qualification_sha256: str,
        client_contract: dict[str, object],
        server_contract: dict[str, object],
    ) -> None:
        _text(profile_id)
        _integer(revision, 1)
        digest = _text(qualification_sha256)
        if len(digest) != 64 or any(c not in _HEX for c in digest):
            raise InvalidIdentity("invalid source qualification digest")
        client = _object(client_contract, {"platforms", "mount"})
        platforms = client["platforms"]
        if type(platforms) is not list or not platforms:
            raise InvalidIdentity("profile platforms must be nonempty records")
        for platform in cast(list[object], platforms):
            record = _object(
                platform, {"arch", "kernel_release", "libc_version", "encoder_revision"}
            )
            for value in record.values():
                _text(value)
        mount = _object(
            client["mount"],
            {"filesystem", "nfs_version", "transport", "security", "hard"},
        )
        for key in ("filesystem", "nfs_version", "transport", "security"):
            _text(mount[key])
        if type(mount["hard"]) is not bool:
            raise InvalidIdentity("profile hard-mount setting must be boolean")
        server = _object(
            server_contract,
            {
                "vendor",
                "version",
                "filesystem",
                "exporter",
                "handle_contract",
                "allowed_transitions",
                "recovery_transitions",
            },
        )
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "qualification_sha256", qualification_sha256)
        object.__setattr__(self, "_client_json", _contract_json(client))
        object.__setattr__(self, "_server_json", _contract_json(server))

    @property
    def client_contract(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self._client_json))

    @property
    def server_contract(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self._server_json))


# Future records must be immutable source-qualified snapshots, not candidate
# objects. Task 1 deliberately provides neither records nor a registration API.
_QUALIFIED_PROFILES: tuple[StorageProfile, ...] = ()


def validate_profile(profile: StorageProfile) -> None:
    if type(profile) is not StorageProfile:
        raise InvalidIdentity("expected a storage profile candidate")
    if profile not in _QUALIFIED_PROFILES:
        raise IdentityUnavailable("storage profile has no source qualification")


@dataclass(frozen=True, slots=True)
class NativeHandle:
    handle_type: int
    handle_bytes: bytes

    def __post_init__(self) -> None:
        _integer(self.handle_type, _INT32_MIN, _INT32_MAX)
        if type(self.handle_bytes) is not bytes or not (
            1 <= len(self.handle_bytes) <= _MAX_HANDLE_BYTES
        ):
            raise InvalidIdentity("native handle bytes are outside the fixed bound")

    def to_value(self) -> dict[str, object]:
        return {
            "version": 1,
            "backend": "linux-exportfs",
            "handle_type": self.handle_type,
            "handle_size": len(self.handle_bytes),
            "handle_hex": self.handle_bytes.hex(),
        }

    @classmethod
    def from_value(cls, value: object) -> NativeHandle:
        fields = _object(
            value, {"version", "backend", "handle_type", "handle_size", "handle_hex"}
        )
        _integer(fields["version"], 1, 1)
        if _text(fields["backend"]) != "linux-exportfs":
            raise InvalidIdentity("unsupported identity backend")
        handle_type = _integer(fields["handle_type"], _INT32_MIN, _INT32_MAX)
        size = _integer(fields["handle_size"], 1, _MAX_HANDLE_BYTES)
        encoded = _text(fields["handle_hex"])
        if len(encoded) != size * 2 or any(c not in _HEX for c in encoded):
            raise InvalidIdentity("invalid native handle hex or size")
        return cls(handle_type, bytes.fromhex(encoded))

    @classmethod
    def from_json(cls, encoded: str) -> NativeHandle:
        if type(encoded) is not str:
            raise InvalidIdentity("identity JSON must be text")
        try:
            value: object = json.loads(
                encoded, object_pairs_hook=_pairs, parse_constant=_constant
            )
        except (ValueError, TypeError, RecursionError) as exc:
            raise InvalidIdentity("invalid identity JSON") from exc
        return cls.from_value(value)


@dataclass(frozen=True, slots=True)
class OwnerRef:
    domain: str
    key: tuple[str | int, ...]

    def __post_init__(self) -> None:
        if _text(self.domain) not in _OWNER_DOMAINS:
            raise InvalidIdentity("unknown identity owner domain")
        if type(self.key) is not tuple or not self.key:
            raise InvalidIdentity("owner key must be a nonempty canonical tuple")
        for component in self.key:
            if type(component) is str:
                _text(component)
            elif type(component) is not int:
                raise InvalidIdentity("invalid owner key component")


@dataclass(frozen=True, slots=True)
class RoleKey:
    file_id: int | None
    purpose: str
    kind: str
    relative: str | None

    def __post_init__(self) -> None:
        if self.file_id is not None:
            _integer(self.file_id, 0)
        if not _text(self.purpose).strip():
            raise InvalidIdentity("identity role purpose must not be blank")
        if _text(self.kind) not in {"file", "directory", "marker"}:
            raise InvalidIdentity("invalid identity role kind")
        if self.relative is not None:
            relative = _text(self.relative)
            if "\\" in relative or any(
                part in {"", ".", ".."} for part in relative.split("/")
            ):
                raise InvalidIdentity("identity role path is not canonical relative")


class _FileHandle(ctypes.Structure):
    _fields_ = [
        ("handle_bytes", ctypes.c_uint32),
        ("handle_type", ctypes.c_int32),
        ("f_handle", ctypes.c_ubyte * _MAX_HANDLE_BYTES),
    ]


def capture_native(fd: int) -> NativeHandle:
    """Capture one bounded handle from a borrowed FD without following a path.

    Never closes the FD, opens a handle, requests privilege, or retries with
    alternate flags. The mount ID is transient output, not durable identity.
    """
    _integer(fd, 0, _INT32_MAX)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        name_to_handle = libc.name_to_handle_at
    except (AttributeError, OSError) as exc:
        raise IdentityUnavailable("native identity libc symbol is unavailable") from exc
    name_to_handle.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.POINTER(_FileHandle),
        ctypes.POINTER(ctypes.c_int32),
        ctypes.c_int,
    ]
    name_to_handle.restype = ctypes.c_int
    handle = _FileHandle()
    handle.handle_bytes = _MAX_HANDLE_BYTES
    mount_id = ctypes.c_int32()
    result = name_to_handle(
        fd, b"", ctypes.byref(handle), ctypes.byref(mount_id), _AT_EMPTY_PATH
    )
    error = ctypes.get_errno()
    if result != 0:
        raise IdentityUnavailable(f"native identity capture failed (errno {error})")
    if not 1 <= handle.handle_bytes <= _MAX_HANDLE_BYTES:
        raise IdentityUnavailable("native identity exceeds the fixed handle bound")
    return NativeHandle(
        handle.handle_type, bytes(handle.f_handle[: handle.handle_bytes])
    )
