"""Owned rollback FD ordering, with explicitly simulated NFS hidden entries."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from collections.abc import Iterator

import pytest

import conftest  # noqa: F401
import chapter_provenance as provenance

_CARRIER = ".mangarr-claim-chapter-rollback-"
_PAYLOAD = b"chapter bytes for descriptor lifecycle"
_Path = str | os.PathLike[str]
_Identity = tuple[int, int]


class _DisposalObservation:
    """Real helper/syscall paths; model only NFS's last-file-close entry lifetime."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, simulated_nfs: bool) -> None:
        self.events: list[tuple[str, _Identity]] = []
        self.ghosts: dict[int, Path] = {}
        owners: dict[int, _Identity] = {}
        captured: set[_Identity] = set()
        validated: set[_Identity] = set()
        carriers: dict[str, _Identity] = {}
        real_matches = provenance._OwnedCopy.matches
        real_rename, real_unlink, real_close, real_rmdir = (
            os.rename,
            os.unlink,
            os.close,
            os.rmdir,
        )

        def matches(owner: provenance._OwnedCopy, expected: tuple[int, ...]) -> bool:
            result = real_matches(owner, expected)
            if result:
                assert owner._descriptor is not None
                key = (expected[0], expected[1])
                owners[owner._descriptor] = key
                if key in captured:
                    validated.add(key)
                    self.events.append(("validated", key))
            return result

        def rename(
            src: _Path,
            dst: _Path,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            if dst == "artifact":
                info = os.stat(src, dir_fd=src_dir_fd, follow_symlinks=False)
                key = (info.st_dev, info.st_ino)
                assert key in owners.values(), "owner pin lost before capture"
                self.events.append(("capture", key))
                captured.add(key)
            real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        def unlink(path: _Path, *, dir_fd: int | None = None) -> None:
            if path != "artifact":
                return real_unlink(path, dir_fd=dir_fd)
            assert dir_fd is not None
            info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            key = (info.st_dev, info.st_ino)
            fd = next(fd for fd, held in owners.items() if held == key)
            held = os.fstat(fd)
            assert (held.st_dev, held.st_ino) == key
            assert key in validated, "unlink preceded live-owner capture validation"
            real_unlink(path, dir_fd=dir_fd)
            self.events.append(("unlink", key))
            carrier = Path(os.readlink(f"/proc/self/fd/{dir_fd}"))
            carriers[carrier.name] = key
            if simulated_nfs:
                ghost = carrier / ".nfs-simulated-owned-pin"
                ghost.write_bytes(b"simulated NFS hidden entry")
                self.ghosts[fd] = ghost

        def close(fd: int) -> None:
            real_close(fd)
            key = owners.pop(fd, None)
            if key is not None:
                self.events.append(("close", key))
            ghost = self.ghosts.pop(fd, None)
            if ghost is not None:
                real_unlink(ghost)

        def rmdir(path: _Path, *, dir_fd: int | None = None) -> None:
            key = carriers.get(os.path.basename(path))
            if key is not None:
                self.events.append(("rmdir", key))
            real_rmdir(path, dir_fd=dir_fd)

        monkeypatch.setattr(provenance._OwnedCopy, "matches", matches)
        monkeypatch.setattr(os, "rename", rename)
        monkeypatch.setattr(os, "unlink", unlink)
        monkeypatch.setattr(os, "close", close)
        monkeypatch.setattr(os, "rmdir", rmdir)

    def assert_disposed_before_rmdir(self) -> None:
        assert not self.ghosts
        keys = {key for event, key in self.events if event == "unlink"}
        assert keys
        for key in keys:
            events = [event for event, identity in self.events if identity == key]
            assert (
                events.index("capture")
                < events.index("validated")
                < events.index("unlink")
            )
            assert (
                events.index("unlink") < events.index("close") < events.index("rmdir")
            )


@pytest.fixture
def owner() -> Iterator[provenance._OwnedCopy]:
    owned = provenance._OwnedCopy()
    try:
        yield owned
    finally:
        owned.close()
        owned.close()


@pytest.mark.parametrize("simulated_nfs", [False, True], ids=["local", "simulated-nfs"])
@pytest.mark.parametrize("limit", [1, 10_000])
@pytest.mark.parametrize("flow", ["nondeferred", "failed-copy", "deferred-rollback"])
def test_owned_disposal_releases_only_unlinked_pin_before_carrier_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: provenance._OwnedCopy,
    flow: str,
    limit: int,
    simulated_nfs: bool,
) -> None:
    source, destination = tmp_path / "source.cbz", tmp_path / "chapter.cbz"
    source.write_bytes(_PAYLOAD)
    observation = _DisposalObservation(monkeypatch, simulated_nfs=simulated_nfs)
    monkeypatch.setattr(provenance, "FINGERPRINT_MAX_BYTES", limit)
    if flow == "failed-copy":
        real_write = os.write
        partial = False

        def write(fd: int, data: bytes | bytearray | memoryview) -> int:
            nonlocal partial
            if fd == owner._descriptor:
                if partial:
                    raise OSError(errno.EIO, "injected partial-copy write failure")
                partial = True
                return real_write(fd, memoryview(data)[:3])
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", write)
        with pytest.raises(OSError, match="partial-copy write failure"):
            provenance.copy_new_file(str(source), str(destination), _owner=owner)
        assert partial and not destination.exists()
    else:
        evidence, verified = provenance.copy_new_file(
            str(source),
            str(destination),
            _owner=owner,
            _defer_retirement=flow == "deferred-rollback",
        )
        assert verified is (limit > len(_PAYLOAD))
        assert destination.read_bytes() == _PAYLOAD
        assert owner.matches((evidence.dev, evidence.inode))
        assert owner._intent is not None
        intent = owner._intent
        if flow == "deferred-rollback":
            assert provenance.remove_if_unchanged(
                str(destination), evidence, _owner=owner
            )
            assert owner._descriptor is None
            assert owner._intent is intent
            owner.verify_parent()
            os.fstat(intent.parent_fd)
            assert not intent.retired
            owner.retire_pending()
            assert not destination.exists()
        else:
            assert owner._descriptor is not None, "retiring marker closed chapter pin"
        assert intent.retired and intent.marker._descriptor is None
        os.fstat(intent.parent_fd)
    owner.close()
    owner.close()
    assert source.read_bytes() == _PAYLOAD
    assert not os.path.lexists(provenance._pending_path(str(destination)))
    assert list(tmp_path.glob(_CARRIER + "*")) == []
    observation.assert_disposed_before_rmdir()


@pytest.mark.parametrize("retained", [False, True], ids=["restored", "retained"])
def test_foreign_capture_refusal_does_not_consume_original_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: provenance._OwnedCopy,
    retained: bool,
) -> None:
    import import_publication

    source, destination = tmp_path / "source", tmp_path / "chapter"
    displaced = tmp_path / "displaced"
    source.write_bytes(_PAYLOAD)
    evidence, _ = provenance.copy_new_file(
        str(source),
        str(destination),
        _owner=owner,
        _defer_retirement=True,
    )
    destination.rename(displaced)
    destination.write_bytes(b"foreign bytes")
    if retained:

        def refuse_restore(src: str, dst: str) -> None:
            raise OSError(errno.EOPNOTSUPP, "injected no-clobber restore refusal")

        monkeypatch.setattr(import_publication, "_rename_noreplace", refuse_restore)
    assert not provenance.remove_if_unchanged(str(destination), evidence, _owner=owner)
    assert owner.matches((evidence.dev, evidence.inode))
    owner.verify_parent()
    owner.retain_pending()
    assert owner._intent is not None and not owner._intent.retired
    carriers = list(tmp_path.glob(_CARRIER + "*"))
    if retained:
        assert len(carriers) == 1
        assert (carriers[0] / "artifact").read_bytes() == b"foreign bytes"
        receipt = json.loads((carriers[0] / "receipt.json").read_text())
        assert receipt["origin"] == str(destination)
    else:
        assert not carriers and destination.read_bytes() == b"foreign bytes"
    assert displaced.read_bytes() == source.read_bytes() == _PAYLOAD


@pytest.mark.parametrize(
    "boundary", ["unlink", "post-unlink-fsync", "post-unlink-close"]
)
def test_disposal_error_preserves_intent_without_releasing_an_unlinked_pin_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: provenance._OwnedCopy,
    boundary: str,
) -> None:
    source, destination = tmp_path / "source", tmp_path / "chapter"
    source.write_bytes(_PAYLOAD)
    evidence, _ = provenance.copy_new_file(
        str(source),
        str(destination),
        _owner=owner,
        _defer_retirement=True,
    )
    assert owner._descriptor is not None and owner._intent is not None
    descriptor, intent = owner._descriptor, owner._intent
    real_unlink, real_fsync, real_close = os.unlink, os.fsync, os.close
    removed = False

    def unlink(path: _Path, *, dir_fd: int | None = None) -> None:
        nonlocal removed
        if path == "artifact" and boundary == "unlink":
            raise OSError(errno.EIO, "injected unlink refusal")
        real_unlink(path, dir_fd=dir_fd)
        if path == "artifact":
            removed = True

    def fsync(fd: int) -> None:
        if removed and boundary == "post-unlink-fsync":
            raise OSError(errno.EIO, "injected post-unlink sync refusal")
        real_fsync(fd)

    def close(fd: int) -> None:
        real_close(fd)
        if fd == descriptor and removed and boundary == "post-unlink-close":
            raise OSError(errno.EIO, "injected close error after real close")

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", unlink)
        patch.setattr(os, "fsync", fsync)
        patch.setattr(os, "close", close)
        assert not provenance.remove_if_unchanged(
            str(destination), evidence, _owner=owner
        )
    assert owner._intent is intent
    os.fstat(intent.parent_fd)
    owner.verify_parent()
    owner.retain_pending()
    if boundary == "unlink":
        assert owner._descriptor == descriptor and owner.matches(
            (evidence.dev, evidence.inode)
        )
        assert destination.read_bytes() == _PAYLOAD
    else:
        assert owner._descriptor is None
        assert not destination.exists()
    assert not intent.retired
    assert os.path.lexists(provenance._pending_path(str(destination)))
