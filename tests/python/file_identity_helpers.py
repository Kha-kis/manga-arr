"""Simulated identity/media fixtures. These never qualify a native backend."""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType


class SimulatedIdentityBackend:
    """Assign opaque IDs independently of file bytes, retaining inode lifetimes."""

    def __init__(self) -> None:
        self._objects: dict[tuple[int, int], bytes] = {}
        self._descriptors: list[int] = []

    def __enter__(self) -> SimulatedIdentityBackend:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for fd in self._descriptors:
            os.close(fd)
        self._descriptors.clear()
        self._objects.clear()

    def capture(self, path: Path) -> bytes:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            current = os.fstat(fd)
            key = (current.st_dev, current.st_ino)
            if key not in self._objects:
                self._objects[key] = b"simulated-object-" + len(self._objects).to_bytes(
                    8, "big"
                )
                self._descriptors.append(fd)
                fd = -1
            return self._objects[key]
        finally:
            if fd >= 0:
                os.close(fd)


class MediaDeviceProxy:
    """A module-local stat facade, never a patch to the shared os module."""

    def __init__(self, media_root: Path, *, device: int) -> None:
        self._media_root = media_root.resolve()
        self._device = device

    def stat(self, path: str | os.PathLike[str]) -> os.stat_result:
        result = os.stat(path)
        if not Path(path).resolve().is_relative_to(self._media_root):
            return result
        values = list(result)
        values[2] = self._device
        return os.stat_result(
            values,
            {
                "st_atime": result.st_atime,
                "st_mtime": result.st_mtime,
                "st_ctime": result.st_ctime,
                "st_atime_ns": result.st_atime_ns,
                "st_mtime_ns": result.st_mtime_ns,
                "st_ctime_ns": result.st_ctime_ns,
            },
        )
