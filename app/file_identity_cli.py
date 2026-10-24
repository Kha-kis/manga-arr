"""Linux-only private C8 I/O, with no router, parser or application imports.

Use one `_PrivateIO` context for the complete read/stream/final-validation
operation. Inputs and root-to-leaf ancestry stay pinned until context exit.
Every refusal poisons the context and closes all owned descriptors. Failed
outputs are deliberately retained; this module never unlinks or repairs paths.
Callers must exit the context successfully before publishing any success result.

The limit (at most 16 MiB) applies to each input AND their aggregate bytes,
including a cap+1 probe charged before retention. Output has an independent
same-sized ceiling, charged before each bounded write. No parser/escaping or
whole-plan size validation is provided: the future serializer must preflight
the complete escaped plan against its own 16 MiB ceiling. Iterables and their
resources remain caller-owned; descriptors are never returned to callers.

Requires Linux O_PATH, atomic O_EXCL/CLOEXEC/NOFOLLOW, trustworthy fstat/stat,
and genuine procfs `/proc/self/fd` pinned-inode reopen semantics. No pathname
reopen fallback. Same-EUID/root malicious actors are outside the privacy trust
boundary; boundary checks detect substitutions, not atomic namespace snapshots
or immunity to post-validation mutation. Exclusions are explicit caller metadata,
never discovered through config, SQLite or guard acquisition.
"""

from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
import os
import stat
from types import TracebackType
from typing import Literal

_open = os.open
_close = os.close
_read = os.read
_write = os.write
_fsync = os.fsync
_Code = Literal["ownership_private_io", "ownership_budget"]
_LIMIT = 16 * 1024 * 1024
_CHUNK = 64 * 1024
_EMPTY_IDENTITIES: frozenset[tuple[int, int]] = frozenset()


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _directory_state(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_mode, info.st_uid, info.st_gid)


def _file_state(info: os.stat_result) -> tuple[int, ...]:
    return (
        *_directory_state(info),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _path_parts(path: str) -> list[str]:
    if (
        not path.startswith("/")
        or path == "/"
        or "\\" in path
        or "\0" in path
        or any(0xD800 <= ord(c) <= 0xDFFF for c in path)
    ):
        raise _PrivateIOError() from None
    parts = path[1:].split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _PrivateIOError() from None
    return parts


@dataclass(frozen=True, slots=True)
class _Directory:
    fd: int
    parent: int
    name: str
    before: os.stat_result


@dataclass(slots=True)
class _File:
    fd: int
    parent: int
    name: str
    path: str
    before: os.stat_result
    readable: int | None = None


class _PrivateIOError(Exception):
    def __init__(self, code: _Code = "ownership_private_io") -> None:
        self.code: _Code = code
        super().__init__(code)


class _PrivateIO:
    def __init__(
        self,
        *,
        excluded_paths: tuple[str, ...] = (),
        excluded_identities: frozenset[tuple[int, int]] = _EMPTY_IDENTITIES,
        byte_limit: int = _LIMIT,
    ) -> None:
        self._excluded_paths: tuple[str, ...] = excluded_paths
        self._excluded_ids: frozenset[tuple[int, int]] = excluded_identities
        self._limit: int = byte_limit
        self._controls: dict[str, tuple[int, int] | None] = {}
        self._fds: list[int] = []
        self._directories: list[_Directory] = []
        self._files: list[_File] = []
        self._used: int = 0
        self._entered: bool = False
        self._active: bool = False
        self._output_started: bool = False
        self._failure: _Code | None = None

    def __enter__(self) -> "_PrivateIO":
        if self._entered:
            raise _PrivateIOError() from None
        self._entered = self._active = True
        with self._operation():
            if (
                type(self._limit) is not int
                or not 0 < self._limit <= _LIMIT
                or not all(
                    getattr(os, flag, 0)
                    for flag in (
                        "O_PATH",
                        "O_NOFOLLOW",
                        "O_CLOEXEC",
                        "O_DIRECTORY",
                        "O_NONBLOCK",
                    )
                )
                or os.open not in os.supports_dir_fd
                or os.stat not in os.supports_dir_fd
                or os.stat not in os.supports_follow_symlinks
            ):
                raise _PrivateIOError() from None
            for path in self._excluded_paths:
                _ = _path_parts(path)
                self._controls[path] = self._control_identity(path)
            for device, inode in self._excluded_ids:
                if (
                    type(device) is not int
                    or type(inode) is not int
                    or device < 0
                    or inode < 0
                ):
                    raise _PrivateIOError() from None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                if self._failure is not None:
                    raise _PrivateIOError(self._failure) from None
                if self._active:
                    self.validate()
        finally:
            if not self._finish() and exc_type is None:
                self._failure = self._failure or "ownership_private_io"
                raise _PrivateIOError() from None

    def _finish(self) -> bool:
        self._active = False
        clean = True
        while self._fds:
            fd = self._fds.pop()
            try:
                _close(fd)
            except OSError:
                # Linux close errors release the fd; retry could close a reused fd.
                clean = False
        return clean

    @contextmanager
    def _operation(self) -> Generator[None, None, None]:
        try:
            if not self._active:
                raise _PrivateIOError() from None
            yield
        except _PrivateIOError as refusal:
            self._failure = self._failure or refusal.code
            _ = self._finish()
            raise refusal from None
        except Exception:
            self._failure = self._failure or "ownership_private_io"
            _ = self._finish()
            raise _PrivateIOError() from None
        except BaseException:
            self._failure = self._failure or "ownership_private_io"
            _ = self._finish()
            raise

    def _own(self, path: str, flags: int, *, parent: int | None = None) -> int:
        fd = _open(path, flags | os.O_CLOEXEC, 0o600, dir_fd=parent)
        self._fds.append(fd)
        if os.get_inheritable(fd):
            raise _PrivateIOError() from None
        return fd

    @staticmethod
    def _control_identity(path: str) -> tuple[int, int] | None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise _PrivateIOError() from None
        return _identity(info)

    def _reject_control(self, path: str, info: os.stat_result | None = None) -> None:
        if path in self._controls:
            raise _PrivateIOError() from None
        identities = set(self._excluded_ids)
        for control, before in self._controls.items():
            current = self._control_identity(control)
            if current != before:
                raise _PrivateIOError() from None
            if current is not None:
                identities.add(current)
        if info is not None and _identity(info) in identities:
            raise _PrivateIOError() from None

    @staticmethod
    def _regular(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise _PrivateIOError() from None

    @staticmethod
    def _directory_policy(info: os.stat_result, *, immediate: bool) -> bool:
        mode, uid = stat.S_IMODE(info.st_mode), os.geteuid()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid):
            raise _PrivateIOError() from None
        if immediate:
            if info.st_uid != uid or mode != 0o700:
                raise _PrivateIOError() from None
            return False
        if mode & 0o022:
            if info.st_uid != 0 or not mode & stat.S_ISVTX:
                raise _PrivateIOError() from None
            return True
        return False

    def _parent(self, path: str) -> tuple[int, str]:
        parts = _path_parts(path)
        self._reject_control(path)
        self._validate()
        parent = self._own("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root = os.fstat(parent)
        sticky = self._directory_policy(root, immediate=len(parts) == 1)
        self._directories.append(_Directory(parent, parent, ".", root))
        for index, name in enumerate(parts[:-1]):
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            next_sticky = self._directory_policy(
                before, immediate=index == len(parts) - 2
            )
            child = self._own(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, parent=parent
            )
            after = os.fstat(child)
            if _directory_state(before) != _directory_state(after) or (
                sticky and after.st_uid != os.geteuid()
            ):
                raise _PrivateIOError() from None
            self._directories.append(_Directory(child, parent, name, after))
            parent, sticky = child, next_sticky
        self._validate()
        return parent, parts[-1]

    def _validate(self) -> None:
        self._reject_control("")
        for entry in self._directories:
            current = os.stat(entry.name, dir_fd=entry.parent, follow_symlinks=False)
            if _directory_state(entry.before) != _directory_state(
                os.fstat(entry.fd)
            ) or _directory_state(entry.before) != _directory_state(current):
                raise _PrivateIOError() from None
        for item in self._files:
            before = _file_state(item.before)
            current = os.stat(item.name, dir_fd=item.parent, follow_symlinks=False)
            held = os.fstat(item.fd)
            self._regular(held)
            self._reject_control(item.path, held)
            if before != _file_state(held) or before != _file_state(current):
                raise _PrivateIOError() from None
            if item.readable is not None and before != _file_state(
                os.fstat(item.readable)
            ):
                raise _PrivateIOError() from None

    def read_input(self, path: str) -> bytes:
        """Read one private input; retain its pin/read/ancestry until final exit."""
        with self._operation():
            if self._output_started:
                raise _PrivateIOError() from None
            parent, name = self._parent(path)
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            self._regular(before)
            self._reject_control(path, before)
            pin = self._own(name, os.O_PATH | os.O_NOFOLLOW, parent=parent)
            pinned = os.fstat(pin)
            self._regular(pinned)
            self._reject_control(path, pinned)
            if _file_state(before) != _file_state(pinned) or any(
                _identity(item.before) == _identity(pinned) for item in self._files
            ):
                raise _PrivateIOError() from None
            item = _File(pin, parent, name, path, pinned)
            self._files.append(item)
            self._validate()
            if pinned.st_size > self._limit - self._used:
                raise _PrivateIOError("ownership_budget") from None
            # Reopen only the held, verified regular inode, never its raced name.
            self._reject_control(path, os.fstat(pin))
            readable = self._own(f"/proc/self/fd/{pin}", os.O_RDONLY | os.O_NONBLOCK)
            item.readable = readable
            self._validate()
            data = bytearray()
            while True:
                self._validate()
                chunk = _read(readable, min(_CHUNK, self._limit - self._used + 1))
                self._used += len(chunk)
                if self._used > self._limit:
                    raise _PrivateIOError("ownership_budget") from None
                self._validate()
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) != pinned.st_size:
                raise _PrivateIOError() from None
            self._validate()
            return bytes(data)

    def write_output(self, path: str, chunks: Iterable[bytes]) -> int:
        """Exclusively stream one prevalidated report, fsync, then validate all."""
        with self._operation():
            if self._output_started:
                raise _PrivateIOError() from None
            self._output_started = True
            parent, name = self._parent(path)
            fd = self._own(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                parent=parent,
            )
            before = os.fstat(fd)
            self._regular(before)
            self._reject_control(path, before)
            if any(_identity(item.before) == _identity(before) for item in self._files):
                raise _PrivateIOError() from None
            output = _File(fd, parent, name, path, before)
            self._files.append(output)
            total = 0
            self._validate()
            for chunk in chunks:
                self._validate()
                if not isinstance(chunk, bytes):
                    raise _PrivateIOError() from None
                if len(chunk) > self._limit - total:
                    raise _PrivateIOError("ownership_budget") from None
                for offset in range(0, len(chunk), _CHUNK):
                    piece = memoryview(chunk)[offset : offset + _CHUNK]
                    self._validate()
                    size = _write(fd, piece)
                    if size != len(piece):
                        raise _PrivateIOError() from None
                    total += size
                    after = os.fstat(fd)
                    if (
                        _directory_state(before) != _directory_state(after)
                        or after.st_nlink != 1
                        or after.st_size != total
                    ):
                        raise _PrivateIOError() from None
                    output.before = after
                    self._validate()
            self._validate()
            _fsync(fd)
            self._validate()
            _fsync(parent)
            self._validate()
            return total

    def validate(self) -> None:
        """Recheck every input/output name, pinned descriptor and ancestor chain."""
        with self._operation():
            self._validate()


# Read-only C8 routing. Imports of domain/application primitives remain lazy.
import json as _json
import sys as _sys
from contextlib import ExitStack as _ExitStack
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from file_identity_recovery import OwnershipInventory, RecoveryPlan

_OWNERSHIP_CODES = frozenset(
    {
        "ownership_arguments",
        "ownership_private_io",
        "ownership_busy_or_guard",
        "ownership_wire",
        "ownership_budget",
        "ownership_incomplete",
        "ownership_binding",
        "ownership_phase",
        "ownership_context",
        "ownership_authority",
        "ownership_internal",
    }
)
_OWNERSHIP_HELP = (
    "mangarr [--config-dir DIR] ownership inspect --parent PATH --output FILE\n"
    "        [--inventory-version 1|2]\n"
    "        [--max-rows N] [--max-objects N] [--max-hash-bytes N]\n"
    "mangarr [--config-dir DIR] ownership plan --inventory FILE --decisions FILE\n"
    "        --storage-declaration FILE --backup-receipt FILE --output FILE\n"
)


class _OwnershipRefusal(Exception):
    def __init__(self, code: str) -> None:
        self.code: str = code if code in _OWNERSHIP_CODES else "ownership_internal"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class _Invocation:
    command: str
    config_dir: str | None
    options: dict[str, str]
    help: bool = False


def _positive_budget(value: str, maximum: int = 2**63 - 1) -> int:
    if not value or any(c not in "0123456789" for c in value):
        raise _OwnershipRefusal("ownership_arguments") from None
    digits = value.lstrip("0")
    if not digits or len(digits) > 19:
        raise _OwnershipRefusal("ownership_arguments") from None
    number = int(digits)
    if not 0 < number <= maximum:
        raise _OwnershipRefusal("ownership_arguments") from None
    return number


def _parse_invocation(arguments: list[str]) -> _Invocation:
    index = 0
    config: str | None = None
    if arguments and arguments[0].partition("=")[0] == "--config-dir":
        token = arguments[0]
        if "=" in token:
            config = token.split("=", 1)[1]
            index = 1
        elif len(arguments) > 1 and not arguments[1].startswith("-"):
            config = arguments[1]
            index = 2
        else:
            raise _OwnershipRefusal("ownership_arguments") from None
    if index >= len(arguments) or arguments[index] != "ownership":
        raise _OwnershipRefusal("ownership_arguments") from None
    tail = arguments[index + 1 :]
    if tail in (["--help"], ["-h"]):
        return _Invocation("inspect", config, {}, True)
    if not tail or tail[0] not in {"inspect", "plan"}:
        raise _OwnershipRefusal("ownership_arguments") from None
    command = tail[0]
    required = (
        {"--parent", "--output"}
        if command == "inspect"
        else {
            "--inventory",
            "--decisions",
            "--storage-declaration",
            "--backup-receipt",
            "--output",
        }
    )
    numeric: set[str] = (
        {"--max-rows", "--max-objects", "--max-hash-bytes"}
        if command == "inspect"
        else set()
    )
    optional: set[str] = {"--inventory-version"} if command == "inspect" else set()
    options: dict[str, str] = {}
    help_requested = False
    index = 1
    while index < len(tail):
        token = tail[index]
        if token in {"--help", "-h"}:
            if help_requested:
                raise _OwnershipRefusal("ownership_arguments") from None
            help_requested = True
            index += 1
            continue
        option, equal, value = token.partition("=")
        if option not in required | numeric | optional or option in options:
            raise _OwnershipRefusal("ownership_arguments") from None
        index += 1
        if not equal:
            if index >= len(tail) or tail[index].startswith("-"):
                raise _OwnershipRefusal("ownership_arguments") from None
            value = tail[index]
            index += 1
        if option == "--inventory-version" and value not in {"1", "2"}:
            raise _OwnershipRefusal("ownership_arguments") from None
        if option in numeric:
            _ = _positive_budget(
                value, 10000 if option == "--max-objects" else 2**63 - 1
            )
        options[option] = value
    if not help_requested and not required <= options.keys():
        raise _OwnershipRefusal("ownership_arguments") from None
    return _Invocation(command, config, options, help_requested)


def _control_paths(config: str | None) -> tuple[str, ...]:
    directory = config or os.environ.get("MANGARR_CONFIG_DIR", "/config")
    if "\0" in directory or any(0xD800 <= ord(c) <= 0xDFFF for c in directory):
        raise _OwnershipRefusal("ownership_private_io") from None
    database = os.path.join(os.path.abspath(directory), "manga_arr.db")
    parent, name = os.path.dirname(database), os.path.basename(database)
    return (
        database,
        database + "-wal",
        database + "-shm",
        database + "-journal",
        os.path.join(parent, "." + name + ".file-mutation.lock"),
        os.path.join(parent, "." + name + ".ownership-maintenance.lock"),
    )


def _summary(
    inventory: "OwnershipInventory",
    outcome: str,
    digest: str,
    *,
    decisions: int = 0,
    retained: int = 0,
    blocked: int = 0,
) -> dict[str, str | int]:
    return {
        "outcome": outcome,
        "sha256": digest,
        "owners": len(inventory.closure.snapshots),
        "roles": len(inventory.closure.roles),
        "decisions": decisions,
        "retained": retained,
        "blocked": blocked,
    }


def _blocked_owners(plan: "RecoveryPlan") -> int:
    from file_identity_consumers import (
        _Evidence,
        _historical_context,
        _snapshot_records,
    )

    retained = {d.owner for d in plan.decisions if d.action == "retain-blocked"}
    evidence = _Evidence()
    blocked = 0
    for snapshot in plan.inventory.closure.snapshots:
        if snapshot.owner not in retained or snapshot.owner.domain in {
            "namespace",
            "queue_source",
        }:
            continue
        root, _related = _snapshot_records(snapshot, evidence)
        if not _historical_context(snapshot.owner.domain, root):
            blocked += 1
    return blocked


def _write_document(held: _PrivateIO, path: str, value: dict[str, object]) -> None:
    from file_identity import InvalidIdentity
    from file_identity_recovery import _plan_fragments, _wire_size

    # Preflight before output allocation; the existing encoder escapes only
    # bounded fragments, and the held writer charges actual emitted bytes too.
    try:
        size = _wire_size(value)
    except InvalidIdentity:
        raise _OwnershipRefusal("ownership_budget") from None
    chunks = (fragment.encode("ascii") for fragment in _plan_fragments(value))
    if held.write_output(path, chunks) != size:
        raise _OwnershipRefusal("ownership_internal") from None


def _inspect_command(
    invocation: _Invocation, controls: tuple[str, ...]
) -> tuple[int, dict[str, str | int]]:
    from file_identity_consumers import InventoryBudget
    from file_identity_recovery import _path, inspect_ownership
    from file_mutation_lock import FileMutationLockError, file_mutation_guard

    options = invocation.options
    parent = _path(options["--parent"])
    budget = InventoryBudget(
        _positive_budget(options.get("--max-rows", "1000")),
        _positive_budget(options.get("--max-objects", "10000"), 10000),
        _positive_budget(options.get("--max-hash-bytes", "68719476736")),
    )
    with _ExitStack() as stack:
        try:
            guard = stack.enter_context(file_mutation_guard(controls[0]))
        except (FileMutationLockError, OSError):
            raise _OwnershipRefusal("ownership_busy_or_guard") from None
        inventory = inspect_ownership(
            guard,
            parent,
            budget,
            inventory_version=int(options.get("--inventory-version", "1")),
        )
        # SQL-managed coordination may create sidecars. Capture exclusion
        # metadata after the inspector has closed SQL, while its guard stays held.
        with _PrivateIO(excluded_paths=controls) as held:
            _write_document(held, options["--output"], inventory._value())
            guard.verify()
        guard.verify()
    incomplete = bool(inventory.unresolved)
    return (3 if incomplete else 0), _summary(
        inventory,
        "inspect_incomplete" if incomplete else "inspect_complete",
        inventory.sha256,
        blocked=len(inventory.unresolved),
    )


def _plan_command(
    invocation: _Invocation, controls: tuple[str, ...]
) -> tuple[int, dict[str, str | int]]:
    from file_identity_recovery import OwnershipInventory, build_recovery_plan

    options = invocation.options
    with _PrivateIO(excluded_paths=controls) as held:
        try:
            inventory_json, selection, declaration, receipt = (
                held.read_input(options[key]).decode("utf-8")
                for key in (
                    "--inventory",
                    "--decisions",
                    "--storage-declaration",
                    "--backup-receipt",
                )
            )
        except UnicodeError:
            raise _OwnershipRefusal("ownership_wire") from None
        inventory = OwnershipInventory.from_json(inventory_json)
        plan = build_recovery_plan(inventory, selection, declaration, receipt)
        blocked = _blocked_owners(plan)
        summary = _summary(
            inventory,
            "plan_recorded_blocked" if blocked else "plan_recorded",
            plan.sha256,
            decisions=len(plan.decisions),
            retained=len(
                {(d.owner, role) for d in plan.decisions for role in d.retained_roles}
            ),
            blocked=blocked,
        )
        _write_document(held, options["--output"], plan._value())
    return 0, summary


def ownership_main(arguments: list[str]) -> int:
    """Read-only C8 entry, with privacy established before legacy argparse."""
    code: str | None = None
    try:
        invocation = _parse_invocation(arguments)
        if invocation.help:
            _ = _sys.stdout.write(_OWNERSHIP_HELP)
            return 0
        from file_identity import IdentityMismatch, InvalidIdentity
        from file_mutation_lock import FileMutationLockError

        try:
            controls = _control_paths(invocation.config_dir)
            status, summary = (
                _inspect_command(invocation, controls)
                if invocation.command == "inspect"
                else _plan_command(invocation, controls)
            )
        except (IdentityMismatch, InvalidIdentity) as error:
            from file_identity_recovery import _SQL_BUDGET_ERRORS

            message = str(error)
            if message in _SQL_BUDGET_ERRORS | {
                "observation document exceeds byte bound",
                "observation evidence exceeds declared budget",
            }:
                code = "ownership_budget"
            else:
                code = message if message in _OWNERSHIP_CODES else "ownership_wire"
        except FileMutationLockError:
            code = "ownership_busy_or_guard"
        else:
            _ = _sys.stdout.write(
                _json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
            )
            return status
    except _PrivateIOError as error:
        code = error.code
    except _OwnershipRefusal as error:
        code = error.code
    except Exception:
        code = "ownership_internal"
    _ = _sys.stderr.write((code or "ownership_internal") + "\n")
    return 2 if code == "ownership_arguments" else 1
