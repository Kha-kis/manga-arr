"""Private C8 I/O only: real owned files, no router/planner/application calls."""

from __future__ import annotations

from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager, nullcontext
import hashlib
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
from typing import cast

import pytest

import file_identity_cli as io


@pytest.fixture
def private(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    return tmp_path


def put(path: Path, data: bytes = b"private") -> str:
    path.write_bytes(data)
    path.chmod(0o600)
    return str(path)


def metadata(path: Path) -> tuple[object, ...]:
    info = path.lstat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        path.read_bytes() if stat.S_ISREG(info.st_mode) else None,
    )


@pytest.fixture(autouse=True)
def owned_fds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[None]:
    """Track every helper-owned fd, including reused numbers, not process counts."""
    opened: dict[int, tuple[int, int]] = {}
    outputs: set[Path] = set()
    hashes: dict[tuple[int, ...], str] = {}
    real_open, real_close = io._open, io._close
    real_read, real_write, real_sync = io._read, io._write, io._fsync

    def nonoutputs() -> dict[str, tuple[object, ...]]:
        result: dict[str, tuple[object, ...]] = {}
        for path in tmp_path.rglob("*"):
            if path in outputs:
                continue
            info = path.lstat()
            # Directory times/link counts can change only for new output names.
            state = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
            if stat.S_ISREG(info.st_mode):
                key = (
                    *state,
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
                if key not in hashes:
                    try:
                        hashes[key] = hashlib.sha256(path.read_bytes()).hexdigest()
                    except PermissionError:
                        hashes[key] = "unreadable: metadata conservation only"
                result[str(path)] = (*key, hashes[key])
            else:
                result[str(path)] = (
                    *state,
                    os.readlink(path) if path.is_symlink() else None,
                )
        return result

    def opening(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if flags & os.O_CREAT:
            assert dir_fd is not None
            candidate = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / path
            try:
                candidate.lstat()
            except FileNotFoundError:
                outputs.add(candidate)
        before = nonoutputs()
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        assert nonoutputs() == before
        assert fd not in opened
        assert not os.get_inheritable(fd)
        info = os.fstat(fd)
        opened[fd] = (info.st_dev, info.st_ino)
        return fd

    def closing(fd: int) -> None:
        before = nonoutputs()
        assert fd in opened, "close of unowned/already-closed fd"
        assert opened.pop(fd) == (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        real_close(fd)
        assert nonoutputs() == before
        with pytest.raises(OSError):
            os.fstat(fd)

    def reading(fd: int, size: int) -> bytes:
        before = nonoutputs()
        data = real_read(fd, size)
        assert nonoutputs() == before
        return data

    def writing(fd: int, data: bytes | memoryview) -> int:
        before = nonoutputs()
        count = real_write(fd, data)
        assert nonoutputs() == before
        return count

    def syncing(fd: int) -> None:
        before = nonoutputs()
        real_sync(fd)
        assert nonoutputs() == before

    monkeypatch.setattr(io, "_open", opening)
    monkeypatch.setattr(io, "_close", closing)
    monkeypatch.setattr(io, "_read", reading)
    monkeypatch.setattr(io, "_write", writing)
    monkeypatch.setattr(io, "_fsync", syncing)
    yield
    assert opened == {}, "helper leaked owned descriptors"
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@contextmanager
def refuses(code: str = "ownership_private_io") -> Generator[None, None, None]:
    with pytest.raises(io._PrivateIOError) as caught:
        yield
    assert caught.value.code == code
    assert str(caught.value) == code
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_multiple_inputs_stream_success_and_conservation(private: Path) -> None:
    first, second = private / "first", private / "second"
    one, two = put(first, b"one"), put(second, b"two")
    before = [metadata(first), metadata(second)]
    with io._PrivateIO() as held:
        assert held.read_input(one) == b"one"
        assert held.read_input(two) == b"two"
        assert (
            held.write_output(str(private / "report"), iter((b"{", b"}", b"\n"))) == 3
        )
        held.validate()
    assert [metadata(first), metadata(second)] == before
    assert (private / "report").read_bytes() == b"{}\n"
    assert stat.S_IMODE((private / "report").stat().st_mode) == 0o600
    with refuses():
        held.validate()


@pytest.mark.parametrize("mode", [0o000, 0o400, 0o640, 0o666, 0o1600])
def test_wrong_input_mode(private: Path, mode: int) -> None:
    path = private / "input"
    name = put(path)
    path.chmod(mode)
    before = path.lstat()
    with refuses(), io._PrivateIO() as held:
        held.read_input(name)
    assert path.lstat() == before


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "hardlink", "socket"])
def test_input_types_and_links(private: Path, kind: str) -> None:
    path, neighbor = private / "input", private / "neighbor"
    put(neighbor)
    if kind == "symlink":
        path.symlink_to(neighbor)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind == "hardlink":
        os.link(neighbor, path)
    else:
        # Linux AF_UNIX limits names to 108 bytes; retain the real owned path.
        parent = os.open(private, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            with socket.socket(socket.AF_UNIX) as endpoint:
                endpoint.bind(f"/proc/self/fd/{parent}/input")
        finally:
            os.close(parent)
    before = metadata(neighbor)
    with refuses(), io._PrivateIO() as held:
        held.read_input(str(path))
    assert metadata(neighbor) == before


@pytest.mark.parametrize(
    "suffix", ["/../input", "//input", "/./input", "/input/", "/a\\b", "/\0", "/\ud800"]
)
def test_lexical_paths_refused(private: Path, suffix: str) -> None:
    with refuses(), io._PrivateIO() as held:
        held.read_input(str(private) + suffix)


def test_relative_and_root_paths_refused() -> None:
    for path in ("input", "/", ""):
        with refuses(), io._PrivateIO() as held:
            held.read_input(path)


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777, 0o1700])
def test_immediate_parent_policy(private: Path, mode: int) -> None:
    name = put(private / "input")
    private.chmod(mode)
    with refuses(), io._PrivateIO() as held:
        held.read_input(name)


def test_unsafe_and_symlink_ancestor(private: Path) -> None:
    ancestor = private / "ancestor"
    ancestor.mkdir(mode=0o777)
    child = ancestor / "child"
    child.mkdir(mode=0o700)
    name = put(child / "input")
    ancestor.chmod(0o777)
    with refuses(), io._PrivateIO() as held:
        held.read_input(name)
    ancestor.chmod(0o700)
    alias = private / "alias"
    alias.symlink_to(ancestor, target_is_directory=True)
    with refuses(), io._PrivateIO() as held:
        held.read_input(str(alias / "child" / "input"))


def test_no_overwrite_or_input_output_alias(private: Path) -> None:
    path = private / "input"
    name = put(path)
    before = metadata(path)
    with refuses(), io._PrivateIO() as held:
        held.read_input(name)
        held.write_output(name, [b"different"])
    with refuses(), io._PrivateIO() as held:
        held.write_output(name, [b"private"])
    assert metadata(path) == before


def test_duplicate_input_refused(private: Path) -> None:
    name = put(private / "input")
    with refuses(), io._PrivateIO() as held:
        held.read_input(name)
        held.read_input(name)


def test_excluded_controls_never_readable_opened(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = tuple(
        put(private / name)
        for name in (
            "db",
            "db-wal",
            "db-shm",
            "db-journal",
            ".db.file-mutation.lock",
            ".db.ownership-maintenance.lock",
        )
    )
    before = [metadata(Path(path)) for path in controls]
    opened = io._open
    excluded = {
        (Path(path).stat().st_dev, Path(path).stat().st_ino) for path in controls
    }

    def observe(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = opened(path, flags, mode, dir_fd=dir_fd)
        info = os.fstat(fd)
        assert flags & os.O_PATH or (info.st_dev, info.st_ino) not in excluded
        return fd

    monkeypatch.setattr(io, "_open", observe)
    for path in controls:
        with refuses(), io._PrivateIO(excluded_paths=controls) as held:
            held.read_input(path)
        with refuses(), io._PrivateIO(excluded_paths=controls) as held:
            held.write_output(path, [b"no"])
    name = put(private / "input")
    identity = (Path(name).stat().st_dev, Path(name).stat().st_ino)
    with refuses(), io._PrivateIO(excluded_identities=frozenset({identity})) as held:
        held.read_input(name)
    assert [metadata(Path(path)) for path in controls] == before


@pytest.mark.parametrize("size,limit,ok", [(8, 8, True), (9, 8, False), (0, 1, True)])
def test_input_budget(private: Path, size: int, limit: int, ok: bool) -> None:
    name = put(private / "input", b"x" * size)
    if ok:
        with io._PrivateIO(byte_limit=limit) as held:
            assert held.read_input(name) == b"x" * size
    else:
        with refuses("ownership_budget"), io._PrivateIO(byte_limit=limit) as held:
            held.read_input(name)


def test_aggregate_inputs_no_multiplier(private: Path) -> None:
    one, two = put(private / "one", b"1234"), put(private / "two", b"56789")
    with refuses("ownership_budget"), io._PrivateIO(byte_limit=8) as held:
        held.read_input(one)
        held.read_input(two)


@pytest.mark.parametrize(
    "fault", ["partial", "zero", "write", "file_fsync", "parent_fsync", "generator"]
)
def test_stream_faults_leave_partial_and_conserve_inputs(
    private: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    source, output = private / "input", private / "report"
    name = put(source)
    before = metadata(source)
    writing, syncing = io._write, io._fsync

    def write(fd: int, data: bytes | memoryview) -> int:
        if fault == "partial":
            return writing(fd, data[:1])
        if fault == "zero":
            return 0
        if fault == "write":
            raise OSError("secret sentinel")
        return writing(fd, data)

    def fsync(fd: int) -> None:
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (fault == "file_fsync" and not directory) or (
            fault == "parent_fsync" and directory
        ):
            raise OSError("secret sentinel")
        syncing(fd)

    def chunks() -> Iterator[bytes]:
        yield b"abc"
        if fault == "generator":
            raise RuntimeError("secret sentinel")
        yield b"def"

    monkeypatch.setattr(io, "_write", write)
    monkeypatch.setattr(io, "_fsync", fsync)
    with refuses(), io._PrivateIO() as held:
        held.read_input(name)
        held.write_output(str(output), chunks())
    assert metadata(source) == before
    assert output.exists()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "chunks,ok", [((b"1234", b"5678"), True), ((b"1234", b"56789"), False)]
)
def test_stream_budget(private: Path, chunks: tuple[bytes, ...], ok: bool) -> None:
    output = str(private / "report")
    if ok:
        with io._PrivateIO(byte_limit=8) as held:
            assert held.write_output(output, chunks) == 8
    else:
        with refuses("ownership_budget"), io._PrivateIO(byte_limit=8) as held:
            held.write_output(output, chunks)
        assert Path(output).read_bytes() == b"1234"


@pytest.mark.parametrize("replacement", ["fifo", "control"])
@pytest.mark.parametrize("boundary", ["pin", "reopen"])
def test_metadata_to_readable_open_substitution(
    private: Path, monkeypatch: pytest.MonkeyPatch, replacement: str, boundary: str
) -> None:
    path, original, control = (
        private / "input",
        private / "original",
        private / "control",
    )
    name, excluded = put(path, b"original"), put(control, b"control")
    control_before = metadata(control)
    control_id = control.stat().st_dev, control.stat().st_ino
    opened = io._open
    swapped = False

    def race(
        target: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        nonlocal swapped
        trigger = (
            target == "input" and flags & os.O_PATH
            if boundary == "pin"
            else target.startswith("/proc/self/fd/")
        )
        if trigger and not swapped:
            swapped = True
            path.rename(original)
            if replacement == "fifo":
                os.mkfifo(path, 0o600)
            else:
                os.link(control, path)
        fd = opened(target, flags, mode, dir_fd=dir_fd)
        info = os.fstat(fd)
        if not flags & os.O_PATH:
            assert not stat.S_ISFIFO(info.st_mode)
            assert (info.st_dev, info.st_ino) != control_id
        return fd

    monkeypatch.setattr(io, "_open", race)
    with refuses(), io._PrivateIO(excluded_paths=(excluded,)) as held:
        held.read_input(name)
    assert swapped
    assert original.read_bytes() == b"original"
    # Attacker-created link changes control nlink/ctime, never its content/mode.
    assert control.read_bytes() == control_before[-1]
    assert control.stat().st_mode == control_before[2]


@pytest.mark.parametrize(
    "change", ["input", "ancestor", "mode", "growth", "shrink", "mtime", "nlink"]
)
def test_read_boundary_changes(
    private: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    directory = private / "held"
    directory.mkdir(mode=0o700)
    source = directory / "input"
    name = put(source)
    reading = io._read
    changed = False

    def race(fd: int, size: int) -> bytes:
        nonlocal changed
        data = reading(fd, size)
        if not changed:
            changed = True
            if change == "input":
                source.rename(directory / "original")
                put(source)
            elif change == "ancestor":
                directory.rename(private / "old")
                directory.mkdir(mode=0o700)
                put(source)
            elif change == "mode":
                source.chmod(0o400)
            elif change == "growth":
                with source.open("ab") as stream:
                    stream.write(b"growth")
            elif change == "shrink":
                source.write_bytes(b"")
            elif change == "mtime":
                info = source.stat()
                os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
            else:
                os.link(source, directory / "alias")
        return data

    monkeypatch.setattr(io, "_read", race)
    with refuses(), io._PrivateIO() as held:
        held.read_input(name)
    assert changed


@pytest.mark.parametrize(
    "change", ["first_input", "output", "ancestor", "mode", "nlink"]
)
def test_final_validation_after_fsync(
    private: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    directory = private / "held"
    directory.mkdir(mode=0o700)
    first, second = directory / "first", directory / "second"
    one, two = put(first), put(second)
    output = directory / "report"
    syncing = io._fsync
    changed = False

    def race(fd: int) -> None:
        nonlocal changed
        syncing(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not changed:
            changed = True
            if change == "first_input":
                first.rename(directory / "old-input")
                put(first, b"substitute")
            elif change == "output":
                output.rename(directory / "partial")
                put(output, b"substitute")
            elif change == "ancestor":
                directory.rename(private / "old")
                directory.mkdir(mode=0o700)
            elif change == "mode":
                output.chmod(0o640)
            else:
                os.link(output, directory / "alias")

    monkeypatch.setattr(io, "_fsync", race)
    with refuses(), io._PrivateIO() as held:
        held.read_input(one)
        held.read_input(two)
        held.write_output(str(output), [b"report"])
    assert changed
    if change == "output":
        assert output.read_bytes() == b"substitute"
        assert (directory / "partial").read_bytes() == b"report"


def test_context_exception_and_refusal_poison(private: Path) -> None:
    name = put(private / "input")
    with pytest.raises(RuntimeError), io._PrivateIO() as held:
        held.read_input(name)
        raise RuntimeError("caller")
    with refuses(), io._PrivateIO() as held:
        with refuses():
            held.read_input(str(private / "missing"))
        with refuses():
            held.write_output(str(private / "report"), [b"no"])
    assert not (private / "report").exists()


def test_single_input_read_only_success(private: Path) -> None:
    name = put(private / "input")
    before = metadata(Path(name))
    with io._PrivateIO() as held:
        assert held.read_input(name) == b"private"
    assert metadata(Path(name)) == before


@pytest.mark.parametrize("fault", ["open", "read", "stat", "cloexec", "interrupt"])
def test_syscall_failures_close_every_owned_handle(
    private: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    name = put(private / "input")
    before = metadata(Path(name))
    opened, reading = io._open, io._read
    inheritable = os.get_inheritable

    def opening(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path.startswith("/proc/self/fd/") and fault == "open":
            raise OSError("secret sentinel")
        return opened(path, flags, mode, dir_fd=dir_fd)

    def read(fd: int, size: int) -> bytes:
        if fault == "read":
            raise OSError("secret sentinel")
        if fault == "interrupt":
            raise KeyboardInterrupt()
        if fault == "stat":
            Path(name).unlink()
        return reading(fd, size)

    monkeypatch.setattr(io, "_open", opening)
    monkeypatch.setattr(io, "_read", read)
    if fault == "cloexec":
        # The open tracker checks genuine CLOEXEC first; inject at own() check.
        calls = 0

        def report(fd: int) -> bool:
            nonlocal calls
            calls += 1
            return calls % 2 == 0 or inheritable(fd)

        monkeypatch.setattr(os, "get_inheritable", report)
    if fault == "interrupt":
        with pytest.raises(KeyboardInterrupt), io._PrivateIO() as held:
            held.read_input(name)
    else:
        with refuses(), io._PrivateIO() as held:
            held.read_input(name)
    if fault != "stat":
        assert metadata(Path(name)) == before


@pytest.mark.parametrize(
    "flag", ["O_PATH", "O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY", "O_NONBLOCK"]
)
def test_unsupported_fd_guarantees_refuse(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    monkeypatch.setattr(os, flag, 0)
    with refuses(), io._PrivateIO():
        pass


@pytest.mark.parametrize("limit", [0, -1, True, 16 * 1024 * 1024 + 1])
def test_invalid_limits_cannot_raise_ceiling(limit: int) -> None:
    with refuses(), io._PrivateIO(byte_limit=limit):
        pass


def test_default_exact_16mib_and_aggregate_exceeded(private: Path) -> None:
    payload = b"x" * (16 * 1024 * 1024)
    name = put(private / "full", payload)
    tiny = put(private / "tiny", b"x")
    with io._PrivateIO() as held:
        assert held.read_input(name) == payload
        assert held.write_output(
            str(private / "report"),
            (payload[i : i + 65536] for i in range(0, len(payload), 65536)),
        ) == len(payload)
    with refuses("ownership_budget"), io._PrivateIO() as held:
        held.read_input(name)
        held.read_input(tiny)
    with refuses("ownership_budget"), io._PrivateIO() as held:
        held.write_output(str(private / "over"), (payload, b"x"))
    assert (private / "over").stat().st_size == len(payload)


def test_cap_plus_one_charged_before_retention(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = private / "input"
    name = put(path, b"12345678")
    reading = io._read
    sizes: list[int] = []

    def grow(fd: int, size: int) -> bytes:
        sizes.append(size)
        with path.open("ab") as stream:
            stream.write(b"9")
        return reading(fd, size)

    monkeypatch.setattr(io, "_read", grow)
    with refuses("ownership_budget"), io._PrivateIO(byte_limit=8) as held:
        held.read_input(name)
    assert sizes == [9]
    assert held._used == 9


@pytest.mark.parametrize("boundary", ["create", "write", "file_fsync", "iteration_end"])
def test_output_swap_during_stream_never_unlinks_substitute(
    private: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    output, partial = private / "report", private / "partial"
    opened, writing, syncing = io._open, io._write, io._fsync
    changed = False

    def swap() -> None:
        nonlocal changed
        if not changed:
            changed = True
            output.rename(partial)
            put(output, b"substitute")

    def opening(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = opened(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_CREAT and boundary == "create":
            swap()
        return fd

    def write(fd: int, data: bytes | memoryview) -> int:
        count = writing(fd, data)
        if boundary == "write":
            swap()
        return count

    def sync(fd: int) -> None:
        syncing(fd)
        if boundary == "file_fsync" and stat.S_ISREG(os.fstat(fd).st_mode):
            swap()

    def chunks() -> Iterator[bytes]:
        yield b"report"
        if boundary == "iteration_end":
            swap()

    monkeypatch.setattr(io, "_open", opening)
    monkeypatch.setattr(io, "_write", write)
    monkeypatch.setattr(io, "_fsync", sync)
    with refuses(), io._PrivateIO() as held:
        held.write_output(str(output), chunks())
    assert changed and partial.exists()
    assert output.read_bytes() == b"substitute"


def test_context_exit_rechecks_all_read_inputs(private: Path) -> None:
    first, second = private / "first", private / "second"
    one, two = put(first), put(second)
    with refuses(), io._PrivateIO() as held:
        held.read_input(one)
        held.read_input(two)
        first.chmod(0o400)


def test_controls_changed_during_read_refuse(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, control = private / "input", private / "control"
    name = put(source)
    reading = io._read

    def change(fd: int, size: int) -> bytes:
        result = reading(fd, size)
        put(control)
        return result

    monkeypatch.setattr(io, "_read", change)
    with refuses(), io._PrivateIO(excluded_paths=(str(control),)) as held:
        held.read_input(name)


def test_uid_and_device_policy_metadata_unit_simulation(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disclosed metadata unit simulation, not real other-UID/device qualification."""
    path = private / "input"
    put(path)
    info = path.stat()
    geteuid = os.geteuid
    monkeypatch.setattr(os, "geteuid", lambda: info.st_uid + 1)
    with refuses():
        io._PrivateIO._regular(info)
    monkeypatch.setattr(os, "geteuid", geteuid)
    values = list(info)
    values[0] = stat.S_IFCHR | 0o600
    with refuses():
        io._PrivateIO._regular(os.stat_result(values))


def test_sticky_policy_metadata_unit_simulation(private: Path) -> None:
    """Root sticky policy unit check; ordinary ancestry tests use real metadata."""
    values = list(private.stat())
    values[0], values[4] = stat.S_IFDIR | 0o1777, 0
    assert io._PrivateIO._directory_policy(os.stat_result(values), immediate=False)
    values[0] = stat.S_IFDIR | 0o777
    with refuses():
        io._PrivateIO._directory_policy(os.stat_result(values), immediate=False)
    values[0], values[4] = stat.S_IFDIR | 0o1777, os.geteuid()
    if os.geteuid() != 0:
        with refuses():
            io._PrivateIO._directory_policy(os.stat_result(values), immediate=False)


def test_unexpected_stream_type_is_private_refusal(private: Path) -> None:
    class InvalidChunks:
        def __iter__(self) -> Iterator[bytes]:
            raise RuntimeError("secret sentinel")

    with refuses(), io._PrivateIO() as held:
        held.write_output(str(private / "report"), InvalidChunks())


@pytest.mark.parametrize("foreign_child", [False, True])
def test_sticky_intermediary_requires_held_current_uid_child(
    private: Path, monkeypatch: pytest.MonkeyPatch, foreign_child: bool
) -> None:
    """Real fd traversal, root-ownership unit simulation only; no native claim."""
    sticky = private / "sticky"
    sticky.mkdir(mode=0o700)
    child = sticky / "child"
    child.mkdir(mode=0o700)
    leaf = child / "leaf"
    leaf.mkdir(mode=0o700)
    name = put(leaf / "input")
    sticky_id = sticky.stat().st_dev, sticky.stat().st_ino
    child_id = child.stat().st_dev, child.stat().st_ino
    real_stat, real_fstat = os.stat, os.fstat

    def simulate(info: os.stat_result) -> os.stat_result:
        identity = info.st_dev, info.st_ino
        if identity == sticky_id or (identity == child_id and foreign_child):
            values = list(info)
            values[4] = 0
            if identity == sticky_id:
                values[0] = stat.S_IFDIR | 0o1777
            return os.stat_result(values)
        return info

    def observe(
        path: str | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        return simulate(real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks))

    def observe_fd(fd: int) -> os.stat_result:
        return simulate(real_fstat(fd))

    with refuses() if foreign_child else nullcontext(), io._PrivateIO() as held:
        monkeypatch.setattr(os, "stat", observe)
        monkeypatch.setattr(os, "fstat", observe_fd)
        if foreign_child:
            with refuses():
                held.read_input(name)
        else:
            assert held.read_input(name) == b"private"


def test_import_has_no_application_or_file_mutation(private: Path) -> None:
    script = """
import os, sys
sys.path.insert(0, sys.argv[1])
def deny(*args, **kwargs):
    raise AssertionError('mutation during import')
os.open = os.mkdir = os.unlink = os.rename = deny
import file_identity_cli
assert not {'auth', 'shared', 'sqlite3', 'cli', 'file_identity_recovery', 'main', 'schema'} & sys.modules.keys()
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            "-c",
            script,
            str(Path(__file__).parents[2] / "app"),
        ],
        cwd=private,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_nonregular_exclusion_metadata_refuses(private: Path, kind: str) -> None:
    control, source = private / "control", private / "input"
    name = put(source)
    if kind == "symlink":
        control.symlink_to(source)
    elif kind == "fifo":
        os.mkfifo(control, 0o600)
    else:
        control.mkdir(mode=0o700)
    with refuses(), io._PrivateIO(excluded_paths=(str(control),)) as held:
        held.read_input(name)


def test_device_substitution_before_reopen_metadata_simulation(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No owned device is available: simulate mode at real O_PATH fstat boundary."""
    path = private / "input"
    name = put(path)
    identity = path.stat().st_dev, path.stat().st_ino
    opened, real_fstat = io._open, os.fstat
    pin: int | None = None
    readable = False

    def opening(
        target: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        nonlocal pin, readable
        if target.startswith("/proc/self/fd/"):
            readable = True
        fd = opened(target, flags, mode, dir_fd=dir_fd)
        if flags & os.O_PATH:
            pin = fd
        return fd

    def substitute(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if fd == pin:
            assert (info.st_dev, info.st_ino) == identity
            values = list(info)
            values[0] = stat.S_IFCHR | 0o600
            return os.stat_result(values)
        return info

    with refuses(), io._PrivateIO() as held:
        monkeypatch.setattr(io, "_open", opening)
        monkeypatch.setattr(os, "fstat", substitute)
        with refuses():
            held.read_input(name)
    assert pin is not None and not readable


def test_close_error_refuses_and_still_closes_remaining_fds(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = put(private / "input")
    closing = io._close
    failed = False

    def fail_after_real_close(fd: int) -> None:
        nonlocal failed
        closing(fd)
        if not failed:
            failed = True
            raise OSError("secret sentinel")

    monkeypatch.setattr(io, "_close", fail_after_real_close)
    with refuses(), io._PrivateIO() as held:
        assert held.read_input(name) == b"private"
    assert failed


def test_premature_eof_refuses(private: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = put(private / "input")
    reading = io._read

    def empty(fd: int, size: int) -> bytes:
        assert size > 0
        return reading(fd, 0)

    monkeypatch.setattr(io, "_read", empty)
    with refuses(), io._PrivateIO() as held:
        held.read_input(name)


def test_context_cannot_reenter_or_read_after_output(private: Path) -> None:
    name = put(private / "input")
    held = io._PrivateIO()
    with refuses(), held:
        assert held.write_output(str(private / "report"), ()) == 0
        with refuses():
            held.read_input(name)
    with refuses(), held:
        pass
    with refuses(), io._PrivateIO() as other:
        other.write_output(str(private / "second"), ())
        other.write_output(str(private / "third"), ())


@pytest.mark.parametrize("identity", [(-1, 2), (1, -1), (True, 2)])
def test_invalid_excluded_identity_refuses(identity: tuple[int, int]) -> None:
    with refuses(), io._PrivateIO(excluded_identities=frozenset({identity})):
        pass


def test_invalid_chunk_refuses_without_materializing_more(private: Path) -> None:
    chunks = cast(Iterable[bytes], [b"valid", "invalid"])
    with refuses(), io._PrivateIO() as held:
        held.write_output(str(private / "report"), chunks)
    assert (private / "report").read_bytes() == b"valid"


def test_output_metadata_changed_during_write_refuses(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = private / "report"
    writing = io._write

    def change(fd: int, data: bytes | memoryview) -> int:
        size = writing(fd, data)
        path.chmod(0o640)
        return size

    monkeypatch.setattr(io, "_write", change)
    with refuses(), io._PrivateIO() as held:
        held.write_output(str(path), [b"data"])


def test_ancestor_substitution_before_open_refuses(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = private / "parent"
    parent.mkdir(mode=0o700)
    name = put(parent / "input")
    opened = io._open
    swapped = False

    def race(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        nonlocal swapped
        if path == "parent" and not swapped:
            swapped = True
            parent.rename(private / "original")
            parent.mkdir(mode=0o700)
            put(parent / "input")
        return opened(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(io, "_open", race)
    with refuses(), io._PrivateIO() as held:
        held.read_input(name)
    assert swapped


def test_broken_proc_reopen_identity_refuses_before_read(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real opens; inject a broken procfs guarantee, not a pathname fallback."""
    name = put(private / "input", b"original")
    replacement = put(private / "other", b"different")
    opened, reading = io._open, io._read
    reads = 0

    def wrong(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path.startswith("/proc/self/fd/"):
            return opened(replacement, flags, mode, dir_fd=dir_fd)
        return opened(path, flags, mode, dir_fd=dir_fd)

    def observe(fd: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        return reading(fd, size)

    monkeypatch.setattr(io, "_open", wrong)
    monkeypatch.setattr(io, "_read", observe)
    with refuses(), io._PrivateIO() as held:
        held.read_input(name)
    assert reads == 0


@pytest.mark.parametrize(
    "fault", ["input_drift", "file_fsync", "parent_fsync", "budget"]
)
def test_caught_failure_requires_refusal_at_context_exit(
    private: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    source, neighbor, report = (
        private / "input",
        private / "neighbor",
        private / "report",
    )
    name = put(source, b"original")
    put(neighbor, b"untouched")
    source_expected, neighbor_before = metadata(source), metadata(neighbor)
    syncing = io._fsync
    code = "ownership_budget" if fault == "budget" else "ownership_private_io"
    events: list[str] = []
    held = io._PrivateIO(byte_limit=8)

    def fail(fd: int) -> None:
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (fault == "parent_fsync") == directory:
            raise OSError("private-exit-sentinel")
        syncing(fd)

    try:
        with refuses(code):
            with held:
                assert held.read_input(name) == b"original"
                if fault in ("file_fsync", "parent_fsync"):
                    monkeypatch.setattr(io, "_fsync", fail)
                with refuses(code):
                    if fault == "budget":
                        held.write_output(str(report), (b"report", b"too big"))
                    else:
                        assert held.write_output(str(report), (b"report",)) == 6
                        assert fault == "input_drift"
                        source.chmod(0o400)
                        source_expected = metadata(source)
                        held.validate()
                events.append(code)
                assert not held._active and held._fds == []
                report_before = metadata(report)
                with refuses():
                    held.validate()
                with refuses():
                    held.read_input(name)
                with refuses():
                    held.write_output(str(private / "second"), (b"no",))
                assert metadata(report) == report_before
                events.append("block_end")
    finally:
        # These checks run even when pre-fix exit incorrectly succeeds.
        assert events == [code, "block_end"]
        assert metadata(source) == source_expected
        assert metadata(neighbor) == neighbor_before
        assert report.read_bytes() == b"report"
        assert stat.S_IMODE(report.stat().st_mode) == 0o600
        assert not (private / "second").exists()
        assert not held._active and held._fds == []


def test_caught_failure_preserves_propagating_caller_exception(private: Path) -> None:
    source = private / "input"
    name = put(source)
    before = metadata(source)
    marker = RuntimeError("caller exception identity")
    held = io._PrivateIO()
    with pytest.raises(RuntimeError) as caught:
        with held:
            assert held.read_input(name) == b"private"
            with refuses():
                held.read_input(str(private / "missing"))
            assert not held._active and held._fds == []
            raise marker
    assert caught.value is marker
    assert metadata(source) == before
    assert not held._active and held._fds == []


def test_unpoisoned_context_still_exits_normally(private: Path) -> None:
    source, report = private / "input", private / "report"
    name = put(source)
    before = metadata(source)
    with io._PrivateIO() as held:
        assert held.read_input(name) == b"private"
        assert held.write_output(str(report), (b"report",)) == 6
        held.validate()
    assert metadata(source) == before
    assert report.read_bytes() == b"report"
    assert not held._active and held._fds == []


# C8 source integration regressions; reviewed private-I/O definitions above stay exact.
def cli_call(arguments: list[str]) -> tuple[int, str, str]:
    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO
    import cli

    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            status = cli.main(arguments)
        except SystemExit as error:
            status = error.code if type(error.code) is int else 1
    return status, stdout.getvalue(), stderr.getvalue()


def cli_child(
    program: str, arguments: list[str], private: Path
) -> tuple[int, str, str]:
    import json

    with subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", program, *arguments],
        cwd=private,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        stdout, stderr = child.communicate(timeout=40)
        assert child.returncode is not None
        with (Path(os.environ.get("TMPDIR", str(private))) / "cli-children.jsonl").open(
            "a"
        ) as record:
            _ = record.write(
                json.dumps(
                    {"pid": child.pid, "returncode": child.returncode, "waited": True}
                )
                + "\n"
            )
        return child.returncode, stdout, stderr


def cli_process(
    arguments: list[str], private: Path, *, offline: bool = False
) -> tuple[int, str, str]:
    program = """
import sys
sys.path.insert(0, sys.argv[1])
import cli
if sys.argv[2] == 'offline':
    import sqlite3, file_mutation_lock, shared
    def denied(*args, **kwargs):
        raise AssertionError('offline-boundary-sentinel')
    sqlite3.connect = file_mutation_lock.file_mutation_guard = shared.get_db = denied
raise SystemExit(cli.main(sys.argv[3:]))
"""
    return cli_child(
        program,
        [
            str(Path(__file__).parents[2] / "app"),
            "offline" if offline else "normal",
            *arguments,
        ],
        private,
    )


def cli_plan_inputs(
    private: Path, *, inventory_version: int = 1, plan_version: int = 1
) -> list[str]:
    from file_identity_recovery import OwnershipInventory
    from test_file_identity_plan import documents, empty_inventory, linked_documents

    inventory = empty_inventory()
    if inventory_version == 2:
        inventory = OwnershipInventory.create(
            inventory.parent,
            inventory.closure,
            inventory.observed,
            inventory.unresolved,
            inventory.budget,
            inventory_version=2,
        )
    reviews = documents(inventory)
    if plan_version == 2:
        reviews = linked_documents(reviews, [])
    selection, declaration, receipt = reviews
    values = (inventory.to_json(), selection, declaration, receipt)
    names = ("inventory", "decisions", "declaration", "receipt")
    for name, value in zip(names, values, strict=True):
        _ = put(private / name, value.encode("utf-8"))
    return [
        "--config-dir",
        str(private / "unused-config"),
        "ownership",
        "plan",
        "--inventory",
        str(private / "inventory"),
        "--decisions",
        str(private / "decisions"),
        "--storage-declaration",
        str(private / "declaration"),
        "--backup-receipt",
        str(private / "receipt"),
        "--output",
        str(private / "result"),
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["ownership", "inspect", "--private-sentinel=value"],
        ["--private-sentinel=value", "ownership", "inspect"],
        ["--config-dir", "--private-sentinel=value", "ownership", "plan"],
        ["ownership", "inspect", "--max-rows", "private-sentinel"],
    ],
)
def test_cli_privacy_precedes_legacy_parser(arguments: list[str]) -> None:
    status, stdout, stderr = cli_call(arguments)
    assert "private-sentinel" not in stdout + stderr
    assert (status, stdout, stderr) == (2, "", "ownership_arguments\n")


def test_cli_actual_empty_plan_and_offline_subprocess(private: Path) -> None:
    import json
    from file_identity_recovery import RecoveryPlan

    arguments = cli_plan_inputs(private)
    before = {
        name: metadata(private / name)
        for name in ("inventory", "decisions", "declaration", "receipt")
    }
    status, stdout, stderr = cli_process(arguments, private, offline=True)
    assert (status, stderr) == (0, "")
    result = RecoveryPlan.from_json((private / "result").read_text())
    assert json.loads(stdout) == {
        "outcome": "plan_recorded",
        "sha256": result.sha256,
        "owners": 0,
        "roles": 0,
        "decisions": 0,
        "retained": 0,
        "blocked": 0,
    }
    assert stdout.count("\n") == 1
    assert {name: metadata(private / name) for name in before} == before
    assert not (private / "unused-config").exists()


@pytest.mark.parametrize("inventory_version", [None, "1", "2"])
@pytest.mark.parametrize("change", ["complete", "incomplete", "unsupported-revision8"])
def test_cli_actual_schema7_inspect(
    private: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    inventory_version: str | None,
) -> None:
    from contextlib import closing
    import json
    import sqlite3
    from file_identity_schema7 import initialize_schema7
    import shared
    from file_identity_recovery import OwnershipInventory

    config, media = private / "config", private / "media"
    config.mkdir(mode=0o700)
    media.mkdir(mode=0o700)
    database = config / "manga_arr.db"
    monkeypatch.setattr(shared, "DB_PATH", str(database))
    initialize_schema7(database, monkeypatch)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        if change != "complete":
            # Refusal marker on schema7, NOT a genuine schema8/migration fixture.
            revision = 8 if change == "unsupported-revision8" else 6
            _ = connection.execute(f"PRAGMA user_version={revision}")
            connection.commit()
    database_before = database.read_bytes()
    media_before = metadata(media)[:-1]
    status, stdout, stderr = cli_process(
        [
            "--config-dir",
            str(config),
            "ownership",
            "inspect",
            "--parent",
            str(media),
            "--output",
            str(private / "result"),
            *(
                ["--inventory-version", inventory_version]
                if inventory_version is not None
                else []
            ),
        ],
        private,
    )
    assert (status, stderr) == (0 if change == "complete" else 3, "")
    result = OwnershipInventory.from_json((private / "result").read_text())
    assert result.version == int(inventory_version or "1")
    assert result.unresolved == (
        () if change == "complete" else ("reference_uncertain",)
    )
    assert json.loads(stdout) == {
        "outcome": "inspect_complete" if change == "complete" else "inspect_incomplete",
        "sha256": result.sha256,
        "owners": len(result.closure.snapshots),
        "roles": len(result.closure.roles),
        "decisions": 0,
        "retained": 0,
        "blocked": len(result.unresolved),
    }
    assert database.read_bytes() == database_before
    assert metadata(media)[:-1] == media_before
    guard = config / ".manga_arr.db.file-mutation.lock"
    assert guard.read_bytes() == b"" and stat.S_IMODE(guard.stat().st_mode) == 0o600


INVENTORY_VERSION_CASES: tuple[tuple[list[str], int | None], ...] = (
    ([], 1),
    (["--inventory-version", "1"], 1),
    (["--inventory-version=1"], 1),
    (["--inventory-version", "2"], 2),
    (["--inventory-version=2"], 2),
    (["--inventory-version", "0"], None),
    (["--inventory-version", "3"], None),
    (["--inventory-version", "true"], None),
    (["--inventory-version", "False"], None),
    (["--inventory-version", "01"], None),
    (["--inventory-version", "2.0"], None),
    (["--inventory-version", "+2"], None),
    (["--inventory-version", " 2"], None),
    (["--inventory-version", "\u0662"], None),
    (["--inventory-version", "private-sentinel"], None),
    (["--inventory-version="], None),
    (["--inventory-version"], None),
    (["--inventory-v=2"], None),
    (["--inventory-version", "1", "--inventory-version", "2"], None),
    (["--inventory-version=2", "--inventory-version=2"], None),
    (["--inventory-version=1", "--inventory-version", "1"], None),
    (["--inventory-version", "2", "--inventory-version=2"], None),
)

OWNERSHIP_GLOBAL_PREFIXES: tuple[list[str], ...] = (
    ["--private-sentinel=value"],
    ["--config-d=private-sentinel"],
    ["--config-dir", "--help"],
    ["--config-dir", "--private-sentinel=value"],
    ["--config-dir=private-sentinel", "--config-dir=private-sentinel"],
    ["--config-dir", "private-sentinel", "--version"],
)


@pytest.mark.parametrize("prefix", OWNERSHIP_GLOBAL_PREFIXES)
def test_cli_explicit_version_malformed_global_prefix_is_private(
    private: Path, prefix: list[str]
) -> None:
    arguments = [
        *prefix,
        "ownership",
        "inspect",
        "--inventory-version",
        "2",
        "--parent",
        str(private),
        "--output",
        str(private / "result"),
    ]
    assert cli_call(arguments) == (2, "", "ownership_arguments\n")
    assert cli_process(arguments, private) == (2, "", "ownership_arguments\n")
    assert not (private / "result").exists()


@pytest.mark.parametrize("option", ["--inventory-version=2", "--inventory-v=2"])
def test_cli_plan_has_no_inventory_version_override(private: Path, option: str) -> None:
    arguments = [*cli_plan_inputs(private, plan_version=2), option]
    assert cli_call(arguments) == (2, "", "ownership_arguments\n")
    assert cli_process(arguments, private, offline=True) == (
        2,
        "",
        "ownership_arguments\n",
    )
    assert not (private / "result").exists()


@pytest.mark.parametrize("options,version", INVENTORY_VERSION_CASES)
def test_ownership_inventory_version_is_explicit_and_private(
    private: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    version: int | None,
) -> None:
    import json
    from file_identity_recovery import OwnershipInventory

    database, media = cli_schema(private, monkeypatch)
    output = private / "private-sentinel"
    before = database.read_bytes(), metadata(media)
    result = cli_call(
        [
            "--config-dir",
            str(database.parent),
            "ownership",
            "inspect",
            "--parent",
            str(media),
            "--output",
            str(output),
            *options,
        ]
    )
    status, stdout, stderr = result
    assert "private-sentinel" not in stdout + stderr
    if version is None:
        assert result == (2, "", "ownership_arguments\n")
        assert not output.exists()
        assert not (database.parent / ".manga_arr.db.file-mutation.lock").exists()
    else:
        assert (status, stderr) == (0, "")
        inventory = OwnershipInventory.from_json(output.read_text())
        assert inventory.version == version and inventory.unresolved == ()
        assert json.loads(stdout) == {
            "outcome": "inspect_complete",
            "sha256": inventory.sha256,
            "owners": 0,
            "roles": 0,
            "decisions": 0,
            "retained": 0,
            "blocked": 0,
        }
    assert before == (database.read_bytes(), metadata(media))


@pytest.mark.parametrize(
    "arguments",
    [
        ["--config-dir", "ownership", "backup", "validate", "archive"],
        ["--config-dir=ownership", "backup", "validate", "archive"],
        ["--config-d", "ownership", "backup", "validate", "archive"],
        ["backup", "validate", "ownership"],
        ["backup", "restore", "ownership", "--yes"],
    ],
)
def test_cli_legacy_ownership_literals_are_not_rerouted(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    import cli

    seen: list[tuple[str, str]] = []

    def validate(path: str, emit_json: bool) -> int:
        seen.append(("validate", path))
        return 71

    def restore(path: str, config: str, confirmed: bool, parser: object) -> int:
        assert confirmed
        seen.append(("restore", path))
        return 71

    monkeypatch.setattr(cli, "_validate_backup", validate)
    monkeypatch.setattr(cli, "_restore_backup", restore)
    assert cli_call(arguments) == (71, "", "")
    assert len(seen) == 1


@pytest.mark.parametrize(
    "tail",
    [
        [],
        ["apply"],
        ["plan"],
        ["inspect", "extra-private-sentinel"],
        ["inspect", "--par", "/private-sentinel", "--output", "/report"],
        ["inspect", "--parent", "-h", "--output", "/report"],
        ["inspect", "--parent", "/media", "--output", "-h"],
        [
            "inspect",
            "--parent",
            "/media",
            "--parent",
            "/private-sentinel",
            "--output",
            "/report",
        ],
        ["inspect", "--parent", "/media", "--output", "/report", "--max-rows", "0"],
        ["inspect", "--parent", "/media", "--output", "/report", "--max-rows", "-1"],
        ["inspect", "--parent", "/media", "--output", "/report", "--max-rows", "+1"],
        ["inspect", "--parent", "/media", "--output", "/report", "--max-rows", "1.0"],
        [
            "inspect",
            "--parent",
            "/media",
            "--output",
            "/report",
            "--max-rows",
            "\u0661",
        ],
        [
            "inspect",
            "--parent",
            "/media",
            "--output",
            "/report",
            "--max-rows",
            "9223372036854775808",
        ],
        [
            "inspect",
            "--parent",
            "/media",
            "--output",
            "/report",
            "--max-objects",
            "10001",
        ],
        [
            "inspect",
            "--parent",
            "/media",
            "--output",
            "/report",
            "--max-rows",
            "1",
            "--max-rows=1",
        ],
        ["plan", "--help", "--private-sentinel"],
        ["inspect", "--help", "--help"],
    ],
)
def test_cli_strict_arguments_no_effects(
    private: Path, monkeypatch: pytest.MonkeyPatch, tail: list[str]
) -> None:
    import cli

    def no_config(value: str | None) -> str:
        raise AssertionError("legacy config conversion reached")

    monkeypatch.setattr(cli, "_config_dir", no_config)
    assert cli_call(
        ["--config-dir", str(private / "private-sentinel"), "ownership", *tail]
    ) == (2, "", "ownership_arguments\n")
    assert not (private / "private-sentinel").exists()


@pytest.mark.parametrize(
    "tail",
    [
        ["--help"],
        ["-h"],
        ["inspect", "--help"],
        ["plan", "--help"],
        ["inspect", "--inventory-version", "2", "--help"],
    ],
)
def test_cli_help_is_static_and_does_not_open_paths(
    private: Path, monkeypatch: pytest.MonkeyPatch, tail: list[str]
) -> None:
    def denied(*args: object, **kwargs: object) -> int:
        raise AssertionError("help touched a path")

    monkeypatch.setattr(io, "_open", denied)
    monkeypatch.setattr(io, "_control_paths", denied)
    status, stdout, stderr = cli_call(
        ["--config-dir", "private-sentinel", "ownership", *tail]
    )
    assert status == 0 and stderr == ""
    assert "ownership inspect --parent PATH --output FILE" in stdout
    assert "ownership plan --inventory FILE" in stdout
    assert "[--inventory-version 1|2]" in stdout
    assert "private-sentinel" not in stdout


@pytest.mark.parametrize(
    "change,code",
    [
        ("utf8", "ownership_wire"),
        ("duplicate", "ownership_wire"),
        ("binding", "ownership_binding"),
        ("incomplete", "ownership_incomplete"),
    ],
)
def test_cli_actual_wire_refusals(private: Path, change: str, code: str) -> None:
    from test_file_identity_plan import documents, empty_inventory
    from file_identity_recovery import OwnershipInventory

    arguments = cli_plan_inputs(private)
    if change == "utf8":
        _ = put(private / "inventory", b"\xffprivate-sentinel")
    elif change == "duplicate":
        _ = put(
            private / "decisions", b'{"version":1,"version":1,"private-sentinel":0}'
        )
    elif change == "binding":
        original = (private / "decisions").read_bytes()
        _ = put(
            private / "decisions",
            original.replace(empty_inventory().sha256.encode(), b"f" * 64),
        )
    else:
        empty = empty_inventory()
        incomplete = OwnershipInventory.create(
            empty.parent, empty.closure, (), ("sql_unavailable",), empty.budget
        )
        for name, value in zip(
            ("inventory", "decisions", "declaration", "receipt"),
            (incomplete.to_json(), *documents(incomplete)),
            strict=True,
        ):
            _ = put(private / name, value.encode())
    before = {
        name: metadata(private / name)
        for name in ("inventory", "decisions", "declaration", "receipt")
    }
    assert cli_call(arguments) == (1, "", code + "\n")
    assert not (private / "result").exists()
    assert {name: metadata(private / name) for name in before} == before


@pytest.mark.parametrize(
    "fault",
    [
        "existing",
        "input_alias",
        "excluded",
        "output_fsync",
        "caught_poison",
        "budget",
        "internal",
    ],
)
@pytest.mark.parametrize("version", [1, 2])
def test_cli_integration_faults_no_success(
    private: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fault: str,
    version: int,
) -> None:
    import file_identity_recovery as recovery

    arguments = cli_plan_inputs(
        private, inventory_version=version, plan_version=version
    )
    code = "ownership_private_io"
    if fault == "existing":
        _ = put(private / "result", b"existing-report")
    elif fault == "input_alias":
        arguments[arguments.index("--decisions") + 1] = str(private / "inventory")
    elif fault == "excluded":
        config = private / "unused-config"
        config.mkdir(mode=0o700)
        database = config / "manga_arr.db"
        _ = put(database, b"control-sentinel")
        arguments[arguments.index("--inventory") + 1] = str(database)
        opened = io._open
        identity = database.stat().st_dev, database.stat().st_ino

        def observe(
            path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> int:
            fd = opened(path, flags, mode, dir_fd=dir_fd)
            info = os.fstat(fd)
            assert flags & os.O_PATH or (info.st_dev, info.st_ino) != identity
            return fd

        monkeypatch.setattr(io, "_open", observe)
    elif fault == "output_fsync":

        def fail_sync(fd: int) -> None:
            raise OSError("private-sentinel")

        monkeypatch.setattr(io, "_fsync", fail_sync)
    elif fault == "caught_poison":
        write = io._PrivateIO.write_output

        def caught(held: io._PrivateIO, path: str, chunks: Iterable[bytes]) -> int:
            size = write(held, path, chunks)
            (private / "inventory").chmod(0o400)
            with refuses():
                held.validate()
            return size

        monkeypatch.setattr(io._PrivateIO, "write_output", caught)
    elif fault == "budget":
        code = "ownership_budget"

        class CappedIO(io._PrivateIO):
            def __init__(self, *, excluded_paths: tuple[str, ...] = ()) -> None:
                super().__init__(excluded_paths=excluded_paths, byte_limit=32)

        monkeypatch.setattr(io, "_PrivateIO", CappedIO)
    else:
        code = "ownership_internal"

        def defect(*args: object, **kwargs: object) -> recovery.RecoveryPlan:
            raise RuntimeError("private-sentinel")

        monkeypatch.setattr(recovery, "build_recovery_plan", defect)
    assert cli_call(arguments) == (1, "", code + "\n")
    assert all(
        "private-sentinel" not in record.getMessage() for record in caplog.records
    )
    if fault == "existing":
        assert (private / "result").read_bytes() == b"existing-report"
    elif fault in {"output_fsync", "caught_poison"}:
        assert (private / "result").exists()
    else:
        assert not (private / "result").exists()


from test_file_identity_schema import ledger_path as ledger_path
from test_file_identity_observation import (
    ProducerMedia,
    producer_media as producer_media_fixture,
)

producer_media = producer_media_fixture


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("mode", ["cancel", "blocked", "historical", "context"])
def test_cli_real_owner_plan_counts_and_conservation(
    private: Path,
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    version: int,
) -> None:
    from contextlib import closing
    import json
    import sqlite3
    from file_identity import OwnerRef
    from file_identity_recovery import OwnershipInventory, RecoveryPlan
    from file_mutation_lock import file_mutation_guard
    from private_file_claim import ensure_namespace
    from test_file_identity_plan import (
        capture_inventory,
        chosen,
        domain_unacknowledged_deletion,
        linked_documents,
        review_documents,
    )
    from test_file_identity_observation import producer_rescan, media_snapshot

    env = producer_media
    if mode == "context":
        identifier = domain_unacknowledged_deletion(env)
        with closing(sqlite3.connect(env.db_path)) as db, db:
            _ = db.execute(
                "UPDATE volumes SET download_id='foreign-acquisition' WHERE id=1"
            )
    else:
        identifier = producer_rescan(
            env, "rolled_back" if mode == "historical" else "prepared"
        )
        with file_mutation_guard(str(env.db_path)) as guard:
            with ensure_namespace(guard, str(env.library)):
                pass
    # Existing producer-backed offline fixture capture supplies synthetic native
    # observation tokens only; this is not native/platform qualification.
    inventory = capture_inventory(env, monkeypatch)
    if version == 2:
        inventory = OwnershipInventory.create(
            inventory.parent,
            inventory.closure,
            inventory.observed,
            inventory.unresolved,
            inventory.budget,
            inventory_version=2,
        )
    decisions = (
        chosen(
            inventory, OwnerRef("rescan", (identifier,)), "cancel-unmutated", {"source"}
        )
        if mode == "cancel"
        else chosen(
            inventory,
            OwnerRef("deletion", (identifier,)),
            "keep-current",
            {"target"},
            str(env.source),
        )
        if mode == "context"
        else None
    )
    inputs = review_documents(inventory, decisions)
    if version == 2:
        inputs = linked_documents(inputs, [])
    arguments = cli_plan_inputs(private)
    for name, value in zip(
        ("inventory", "decisions", "declaration", "receipt"),
        (inventory.to_json(), *inputs),
        strict=True,
    ):
        _ = put(private / name, value.encode())
    before = media_snapshot(env.scope), cli_control_bytes(env.db_path, private)
    status, stdout, stderr = cli_process(arguments, private, offline=True)
    if mode == "context":
        assert (status, stdout, stderr) == (1, "", "ownership_context\n")
        assert not (private / "result").exists()
    else:
        assert (status, stderr) == (0, "")
        plan = RecoveryPlan.from_json((private / "result").read_text())
        assert plan.version == version and plan.inventory.version == version
        blocked = int(mode == "blocked")
        assert cast(dict[str, object], json.loads(stdout)) == {
            "outcome": "plan_recorded_blocked" if blocked else "plan_recorded",
            "sha256": plan.sha256,
            "owners": len(inventory.closure.snapshots),
            "roles": len(inventory.closure.roles),
            "decisions": len(plan.decisions),
            "retained": sum(len(d.retained_roles) for d in plan.decisions),
            "blocked": blocked,
        }
    assert (
        media_snapshot(env.scope),
        cli_control_bytes(env.db_path, private),
    ) == before


@pytest.mark.parametrize("value", ["000000000000000000001", "9223372036854775807"])
def test_cli_ascii_decimal_budget_help_accepts_in_range_values(value: str) -> None:
    status, stdout, stderr = cli_call(
        ["ownership", "inspect", "--max-rows", value, "--help"]
    )
    assert status == 0 and stderr == "" and "ownership inspect" in stdout


def test_cli_existing_library_byte_budget_maps_without_raw_text(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from file_identity import InvalidIdentity
    import file_identity_recovery as recovery

    arguments = cli_plan_inputs(private)

    def over_budget(*args: object, **kwargs: object) -> recovery.RecoveryPlan:
        raise InvalidIdentity("observation document exceeds byte bound")

    monkeypatch.setattr(recovery, "build_recovery_plan", over_budget)
    assert cli_call(arguments) == (1, "", "ownership_budget\n")
    assert not (private / "result").exists()


def cli_schema(private: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    from file_identity_schema7 import initialize_schema7
    import shared

    config, media = private / "config", private / "media"
    config.mkdir(mode=0o700)
    media.mkdir(mode=0o700)
    database = config / "manga_arr.db"
    monkeypatch.setattr(shared, "DB_PATH", str(database))
    initialize_schema7(database, monkeypatch)
    return database, media


@pytest.mark.parametrize("version", [None, "1", "2"])
def test_cli_empty_schema8_requires_explicit_inventory2(
    private: Path, monkeypatch: pytest.MonkeyPatch, version: str | None
) -> None:
    import schema
    import shared
    from file_identity_recovery import OwnershipInventory

    config, media = private / "config8", private / "media8"
    config.mkdir(mode=0o700)
    media.mkdir(mode=0o700)
    database = config / "manga_arr.db"
    monkeypatch.setattr(shared, "DB_PATH", str(database))
    schema.init_db()
    before = database.read_bytes()
    status, _, stderr = cli_process(
        [
            "--config-dir",
            str(config),
            "ownership",
            "inspect",
            "--parent",
            str(media),
            "--output",
            str(private / "result"),
            *(["--inventory-version", version] if version is not None else []),
        ],
        private,
    )
    result = OwnershipInventory.from_json((private / "result").read_text())
    assert status == (0 if version == "2" else 3)
    assert stderr == ""
    assert result.version == int(version or "1")
    assert result.unresolved == (() if version == "2" else ("reference_uncertain",))
    assert database.read_bytes() == before


def test_cli_plan2_subprocess_caught_private_refusal_cannot_publish_success(
    private: Path,
) -> None:
    import json

    arguments = cli_plan_inputs(private, inventory_version=2, plan_version=2)
    program = """
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import cli, file_identity_cli as io
arguments = json.loads(sys.argv[2])
original = io._PrivateIO.write_output
def caught(held, path, chunks):
    size = original(held, path, chunks)
    pathlib.Path(arguments[arguments.index('--inventory') + 1]).chmod(0o400)
    try:
        held.validate()
    except io._PrivateIOError as error:
        assert error.code == 'ownership_private_io'
    else:
        raise AssertionError('private-sentinel: expected refusal')
    return size
io._PrivateIO.write_output = caught
raise SystemExit(cli.main(arguments))
"""
    result = cli_child(
        program,
        [str(Path(__file__).parents[2] / "app"), json.dumps(arguments)],
        private,
    )
    assert result == (1, "", "ownership_private_io\n")
    assert "private-sentinel" not in result[1] + result[2]
    assert (private / "result").exists()


def cli_control_bytes(database: Path, private: Path) -> dict[str, object]:
    import json

    program = """
import hashlib, json, pathlib, sys
p = pathlib.Path(sys.argv[1])
names = [p.name, p.name+'-wal', p.name+'-shm', p.name+'-journal', '.'+p.name+'.file-mutation.lock', '.'+p.name+'.ownership-maintenance.lock']
result = {}
for name in names:
    path = p.parent / name
    if path.exists():
        s = path.lstat()
        result[name] = [s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_nlink, hashlib.sha256(path.read_bytes()).hexdigest()]
print(json.dumps(result))
"""
    status, stdout, stderr = cli_child(program, [str(database)], private)
    assert status == 0 and stderr == ""
    return cast(dict[str, object], json.loads(stdout))


def test_cli_wal_inspect_conserves_committed_data_and_controls(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import closing
    import sqlite3
    from file_mutation_lock import file_mutation_guard

    database, media = cli_schema(private, monkeypatch)
    with file_mutation_guard(str(database)):
        pass
    with closing(sqlite3.connect(database)) as holder:
        _ = holder.execute("PRAGMA journal_mode=WAL")
        _ = holder.execute("PRAGMA wal_autocheckpoint=0")
        _ = holder.execute(
            "INSERT INTO series(id,title,search_pattern,folder_name) VALUES(81,'committed-wal','committed-wal','WAL')"
        )
        holder.commit()
        assert Path(str(database) + "-wal").stat().st_size > 32
        before = cli_control_bytes(database, private)
        assert cli_process(
            [
                "--config-dir",
                str(database.parent),
                "ownership",
                "inspect",
                "--parent",
                str(media),
                "--output",
                str(private / "result"),
            ],
            private,
        )[::2] == (0, "")
        after = cli_control_bytes(database, private)
        assert before.keys() == after.keys()
        for name in before:
            if name.endswith("-shm"):
                assert (
                    cast(list[object], before[name])[:-1]
                    == cast(list[object], after[name])[:-1]
                )
            else:
                assert before[name] == after[name]
        assert cast(
            object, holder.execute("SELECT title FROM series WHERE id=81").fetchone()
        ) == ("committed-wal",)
    assert cli_control_bytes(database, private)[".manga_arr.db.file-mutation.lock"]


def test_cli_guard_busy_and_environment_config_precedence(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from file_mutation_lock import file_mutation_guard

    database, media = cli_schema(private, monkeypatch)
    monkeypatch.setenv("MANGARR_CONFIG_DIR", str(database.parent))
    arguments = [
        "ownership",
        "inspect",
        "--parent",
        str(media),
        "--output",
        str(private / "result"),
    ]
    with file_mutation_guard(str(database)):
        assert cli_call(arguments) == (1, "", "ownership_busy_or_guard\n")
    assert not (private / "result").exists()
    status, _stdout, stderr = cli_call(arguments)
    assert status == 0 and stderr == ""
    explicit = [
        "--config-dir",
        str(private / "missing-config"),
        *arguments[:-1],
        str(private / "other"),
    ]
    assert cli_call(explicit) == (1, "", "ownership_busy_or_guard\n")
    assert not (private / "missing-config").exists()


@pytest.mark.parametrize("version", [None, 1, 2])
def test_cli_real_producer_inspection_not_mock_dispatch(
    private: Path, producer_media: ProducerMedia, version: int | None
) -> None:
    from contextlib import closing
    import sqlite3
    from file_identity_recovery import OwnershipInventory
    from test_file_identity_observation import producer_rescan, media_snapshot

    env = producer_media
    _ = producer_rescan(env, "prepared")
    config = private / "copy-config"
    config.mkdir(mode=0o700)
    database = config / "manga_arr.db"
    with (
        closing(sqlite3.connect(env.db_path)) as source,
        closing(sqlite3.connect(database)) as target,
    ):
        source.backup(target)
    before = (
        media_snapshot(env.scope),
        cli_control_bytes(database, private),
        cli_control_bytes(env.db_path, private),
    )
    status, stdout, stderr = cli_process(
        [
            "--config-dir",
            str(config),
            "ownership",
            "inspect",
            "--parent",
            str(env.scope),
            "--output",
            str(private / "result"),
            *(["--inventory-version", str(version)] if version is not None else []),
        ],
        private,
    )
    assert status == 0 and stderr == ""
    inventory = OwnershipInventory.from_json((private / "result").read_text())
    import json

    assert inventory.version == (version or 1)
    assert json.loads(stdout) == {
        "outcome": "inspect_complete",
        "sha256": inventory.sha256,
        "owners": len(inventory.closure.snapshots),
        "roles": len(inventory.closure.roles),
        "decisions": 0,
        "retained": 0,
        "blocked": 0,
    }
    assert "private-sentinel" not in stdout + stderr
    assert any(s.owner.domain == "rescan" for s in inventory.closure.snapshots)
    assert inventory.observed and inventory.unresolved == ()
    assert media_snapshot(env.scope) == before[0]
    for name, value in before[1].items():
        assert cli_control_bytes(database, private)[name] == value
    assert cli_control_bytes(env.db_path, private) == before[2]


_BOOTSTRAP_PROGRAM = """
import importlib.abc
import json
import sys
sys.path.insert(0, sys.argv[1])
import cli
target_module = sys.argv[2]
assert target_module not in sys.modules, 'bootstrap precondition'
class ImportFailure(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == target_module:
            raise PermissionError('bootstrap-import-private-sentinel')
        return None
sys.meta_path.insert(0, ImportFailure())
raise SystemExit(cli.main(json.loads(sys.argv[3])))
"""


@pytest.mark.parametrize(
    "target,form",
    [
        ("file_identity_cli", "plan"),
        ("file_identity_cli", "malformed-prefix"),
        ("file_identity_cli", "help"),
        ("file_identity", "plan"),
        ("file_identity_cli", "inspect2"),
        ("file_identity_cli", "inspect2-malformed-prefix"),
        ("file_identity_cli", "inspect2-help"),
        ("file_identity", "inspect2"),
    ],
)
def test_cli_bootstrap_import_failure_is_private(
    private: Path, target: str, form: str
) -> None:
    import json

    arguments = cli_plan_inputs(private)
    before = {
        name: metadata(private / name)
        for name in ("inventory", "decisions", "declaration", "receipt")
    }
    if form.startswith("inspect2"):
        arguments = [
            "ownership",
            "inspect",
            "--inventory-version",
            "2",
            "--parent",
            str(private),
            "--output",
            str(private / "result"),
        ]
        if form == "inspect2-malformed-prefix":
            arguments.insert(0, "--bootstrap-argv-private-sentinel=value")
        elif form == "inspect2-help":
            arguments.append("--help")
    elif form == "malformed-prefix":
        arguments.insert(0, "--bootstrap-argv-private-sentinel=value")
    elif form == "help":
        arguments = ["ownership", "--help"]
    status, stdout, stderr = cli_child(
        _BOOTSTRAP_PROGRAM,
        [str(Path(__file__).parents[2] / "app"), target, json.dumps(arguments)],
        private,
    )
    assert (status, stdout, stderr) == (1, "", "ownership_internal\n")
    assert "private-sentinel" not in stdout + stderr
    assert {name: metadata(private / name) for name in before} == before
    assert not (private / "unused-config").exists()
    assert not (private / "result").exists()


@pytest.mark.parametrize(
    "arguments,status",
    [
        (["--version"], 0),
        (["--help"], 0),
        (["backup", "validate", "ownership"], 1),
        (["backup", "restore", "ownership"], 2),
        (["--config-dir", "ownership", "backup", "validate", "archive"], 1),
        (["--config-d=ownership", "backup", "validate", "archive"], 1),
        (["--legacy-private-sentinel", "backup", "validate", "archive"], 2),
    ],
)
def test_cli_bootstrap_legacy_compatibility_without_router(
    private: Path, arguments: list[str], status: int
) -> None:
    import json

    module = str(Path(__file__).parents[2] / "app")
    baseline = cli_child(
        _BOOTSTRAP_PROGRAM, [module, "no_fault_module", json.dumps(arguments)], private
    )
    faulted = cli_child(
        _BOOTSTRAP_PROGRAM,
        [module, "file_identity_cli", json.dumps(arguments)],
        private,
    )
    assert baseline[0] == status
    assert faulted == baseline
    if status == 0:
        assert baseline[1] and baseline[2] == ""
    else:
        assert baseline[1] == "" and baseline[2]
    if arguments[0] == "--legacy-private-sentinel":
        assert "--legacy-private-sentinel" in baseline[2]
    assert not (private / "ownership").exists()
