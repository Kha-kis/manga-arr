"""Real local read-only SQL acquisition; native handles are explicitly simulated."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any, cast

import pytest

import file_identity_recovery as observation
from file_identity import IdentityUnavailable, InvalidIdentity, NativeHandle
from file_identity_consumers import InventoryBudget, ReferenceClosure
from file_mutation_lock import (
    FileMutationGuard,
    FileMutationLockError,
    file_mutation_guard,
)
from private_file_claim import ensure_namespace
from test_file_identity_inventory import all_rows
from test_file_identity_observation import (
    ProducerMedia,
    captured_fixture,
    control_snapshot,
    media_snapshot,
    producer_media as producer_media,
    producer_queue,
    producer_rescan,
)
from test_file_identity_schema import ledger_path as ledger_path


@pytest.fixture
def namespace(
    ledger_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    media = tmp_path / "media"
    media.mkdir()
    with file_mutation_guard(str(ledger_path)) as guard:
        with ensure_namespace(guard, str(media)):
            pass
    monkeypatch.setattr(
        observation,
        "capture_native",
        lambda fd: NativeHandle(1, f"simulated-sql-{os.fstat(fd).st_ino}".encode()),
    )
    return media


def inspect(
    path: Path,
    parent: Path,
    budget: InventoryBudget | None = None,
) -> observation.OwnershipInventory:
    with file_mutation_guard(str(path)) as guard:
        result = observation.inspect_ownership(
            guard, str(parent), budget or InventoryBudget()
        )
        guard.verify()
    assert observation.OwnershipInventory.from_json(result.to_json()) == result
    return result


def rows(path: Path) -> object:
    db = sqlite3.connect(path)
    try:
        return all_rows(db)
    finally:
        db.close()


def fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def writer(path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    program = (
        "import json,sqlite3,sys\n"
        "db=sqlite3.connect(sys.argv[1],timeout=0)\n"
        "try:\n db.execute(sys.argv[2],json.loads(sys.argv[3])); db.commit()\n"
        "finally:\n db.close()\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(path), sql, json.dumps(params)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@contextmanager
def sql_actor(path: Path, statements: tuple[str, ...]) -> Iterator[None]:
    program = (
        "import json,sqlite3,sys\n"
        "db=sqlite3.connect(sys.argv[1],timeout=0)\n"
        "try:\n"
        " for sql in json.loads(sys.argv[2]):\n  db.execute(sql)\n"
        " print('ready',flush=True)\n sys.stdin.readline()\n"
        "finally:\n db.close()\n"
    )
    actor = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-u",
            "-c",
            program,
            str(path),
            json.dumps(statements),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert actor.stdout is not None
        assert select.select([actor.stdout], [], [], 10)[0], "actor readiness timeout"
        assert actor.stdout.readline() == "ready\n"
        yield
    finally:
        if actor.poll() is None:
            stdout, stderr = actor.communicate("stop\n", timeout=10)
        else:
            stdout, stderr = actor.communicate(timeout=10)
        assert actor.returncode == 0, stdout + stderr


def trace_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Any], list[Any], list[str]]:
    connect = sqlite3.connect
    connections: list[Any] = []
    cursors: list[Any] = []
    statements: list[str] = []

    class TrackedCursor(observation._ReadCursor):
        explicitly_closed: bool = False

        def __init__(self, connection: sqlite3.Connection) -> None:
            super().__init__(connection)
            cursors.append(self)

        def close(self) -> None:
            self.explicitly_closed = True
            super().close()

    monkeypatch.setattr(observation, "_ReadCursor", TrackedCursor)

    def tracked(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("factory") is not observation._ReadConnection:
            return connect(*args, **kwargs)
        base = kwargs.get("factory", sqlite3.Connection)

        class Tracked(base):
            pass

        kwargs["factory"] = Tracked
        db = connect(*args, **kwargs)
        connections.append(db)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", tracked)
    return connections, cursors, statements


def assert_closed(connections: list[Any], cursors: list[Any]) -> None:
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            sqlite3.Connection.execute(db, "SELECT 1")
    for cursor in cursors:
        assert cursor.explicitly_closed, "owned cursor was not explicitly closed"
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            cursor.execute("SELECT 1")


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_sql_real_namespace_no_application_effects(
    ledger_path: Path,
    namespace: Path,
    journal: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = (f"PRAGMA journal_mode={journal}",)
    if journal == "wal":
        statements += (
            "PRAGMA wal_autocheckpoint=0",
            "UPDATE file_claim_namespaces SET ownership_json=ownership_json||' '",
            "COMMIT",
        )
    with sql_actor(ledger_path, statements):
        if journal == "wal":
            assert Path(str(ledger_path) + "-wal").stat().st_size > 32
        before_rows, before_media = rows(ledger_path), media_snapshot(namespace)
        before = control_snapshot(ledger_path)
        before_fds, before_threads = fds(), threading.enumerate()
        connections, cursors, statements = trace_connections(monkeypatch)
        result = inspect(ledger_path, namespace)
        assert result.unresolved == () and len(result.observed) == 3
        assert len(connections) == 2
        assert_closed(connections, cursors)
        assert fds() == before_fds and threading.enumerate() == before_threads
        after = control_snapshot(ledger_path)
        for name, value in before.items():
            if not name.endswith("-shm"):
                assert after[name] == value
        assert rows(ledger_path) == before_rows
        assert media_snapshot(namespace) == before_media
        assert all(
            s.split()[0] in {"SELECT", "PRAGMA", "BEGIN", "ROLLBACK"}
            for s in statements
        )
        assert not any(
            "checkpoint" in s.lower() or "journal_mode" in s.lower() for s in statements
        )


def test_sql_stopped_wal_actor_missing_coordination_is_sqlite_managed(
    ledger_path: Path,
    namespace: Path,
) -> None:
    # A crash leaves committed WAL; all actor descriptors are gone before setup removes SHM.
    program = (
        "import os,sqlite3,sys; d=sqlite3.connect(sys.argv[1]); "
        "d.execute('PRAGMA journal_mode=WAL'); d.execute('PRAGMA wal_autocheckpoint=0'); "
        "d.execute(\"UPDATE file_claim_namespaces SET ownership_json=ownership_json||' '\"); "
        "d.commit(); os._exit(0)"
    )
    subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(ledger_path)],
        check=True,
        timeout=10,
    )
    shm = Path(str(ledger_path) + "-shm")
    assert shm.exists()
    shm.unlink()  # Stopped disposable actor only; never repair sidecars in product code.
    before = control_snapshot(ledger_path)
    result = inspect(ledger_path, namespace)
    after = control_snapshot(ledger_path)
    assert result.unresolved == ()
    assert before[ledger_path.name] == after[ledger_path.name]
    assert before[ledger_path.name + "-wal"] == after[ledger_path.name + "-wal"]
    assert shm.is_file()  # SQLite's ordinary allowed coordination, not product repair.


@pytest.mark.parametrize(
    "suffix,kind",
    [
        ("-wal", "symlink"),
        ("-shm", "fifo"),
        ("-journal", "directory"),
        ("-wal", "db_alias"),
        ("-shm", "hardlink"),
        ("-journal", "cross_control"),
    ],
)
def test_sql_unsafe_control_refuses_before_connect(
    ledger_path: Path,
    tmp_path: Path,
    suffix: str,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = Path(str(ledger_path) + suffix)
    target = tmp_path / "unintended"
    target.write_bytes(b"untouched")
    if kind == "symlink":
        control.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(control)
    elif kind == "directory":
        control.mkdir()
    elif kind == "db_alias":
        os.link(ledger_path, control)
    elif kind == "cross_control":
        wal = Path(str(ledger_path) + "-wal")
        wal.write_bytes(b"control")
        os.link(wal, control)
    else:
        os.link(target, control)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unsafe control reached SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with pytest.raises(InvalidIdentity):
        inspect(ledger_path, tmp_path)
    assert target.read_bytes() == b"untouched"


def test_sql_db_hardlink_and_symlinked_config_ancestry_refuse(
    ledger_path: Path,
    tmp_path: Path,
) -> None:
    alias = tmp_path / "alias.db"
    os.link(ledger_path, alias)
    with pytest.raises(InvalidIdentity):
        inspect(ledger_path, tmp_path)
    alias.unlink()
    config = tmp_path / "config"
    nested = config / "nested"
    nested.mkdir(parents=True)
    ledger_path.rename(nested / ledger_path.name)
    link = tmp_path / "config-link"
    link.symlink_to(config, target_is_directory=True)
    with pytest.raises(InvalidIdentity):
        inspect(link / "nested" / ledger_path.name, tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        "PRAGMA user_version=5",
        "PRAGMA user_version=7",
        "DROP TABLE file_claim_namespaces",
        "ALTER TABLE import_queue DROP COLUMN lease_owner",
        "ALTER TABLE series DROP COLUMN root_folder_id",
    ],
)
def test_sql_unsupported_or_incomplete_schema_is_incomplete(
    ledger_path: Path,
    change: str,
) -> None:
    db = sqlite3.connect(ledger_path)
    try:
        db.execute(change)
        db.commit()
    finally:
        db.close()
    result = inspect(ledger_path, ledger_path.parent)
    assert result.unresolved == ("reference_uncertain",)
    assert result.observed == ()


def test_sql_busy_first_capture_is_explicit_and_closed(
    ledger_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with file_mutation_guard(str(ledger_path)):
        pass
    with sql_actor(ledger_path, ("PRAGMA journal_mode=DELETE", "BEGIN EXCLUSIVE")):
        before = fds()
        connections, cursors, _ = trace_connections(monkeypatch)
        result = inspect(ledger_path, ledger_path.parent)
        assert result.unresolved == ("sql_unavailable",)
        assert result.observed == ()
        assert_closed(connections, cursors)
        assert fds() == before


def test_sql_every_connection_and_cursor_closed_before_media_with_discriminating_writer(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = sqlite3.connect(ledger_path)
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("BEGIN")
    db.execute("SELECT * FROM file_claim_namespaces").fetchall()
    try:
        with pytest.raises(AssertionError, match="locked"):
            writer(
                ledger_path,
                "INSERT INTO settings(key,value) VALUES('control','before')",
            )
    finally:
        db.rollback()
        db.close()
    connections, cursors, _ = trace_connections(monkeypatch)
    original = observation._hash
    called = False

    def during_hash(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        assert len(connections) == 1
        assert_closed(connections, cursors)
        writer(
            ledger_path,
            "INSERT OR REPLACE INTO settings(key,value) VALUES('control','during')",
        )
        called = True
        return original(*args, **kwargs)

    monkeypatch.setattr(observation, "_hash", during_hash)
    result = inspect(ledger_path, namespace)
    assert called and result.unresolved == ()
    assert len(connections) == 2
    assert_closed(connections, cursors)


@pytest.mark.parametrize("change", ["row", "phantom", "context", "lease"])
def test_sql_full_recapture_detects_rows_phantoms_context_and_leases(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    env = producer_media
    producer_queue(env)
    producer_rescan(env, "prepared")
    monkeypatch.setattr(
        observation, "capture_native", lambda fd: NativeHandle(1, b"simulated-drift")
    )
    if change == "row":
        sql, params = (
            "UPDATE rescan_file_operations SET diagnostic='changed raw-only metadata'",
            (),
        )
    elif change == "context":
        sql, params = "UPDATE series SET title='Changed context'", ()
    elif change == "lease":
        sql, params = "UPDATE import_queue SET lease_owner='new-lease'", ()
    else:
        sql, params = "INSERT INTO series_tags(series_id,tag) VALUES(7,'phantom')", ()
    original = observation._hash
    called = False

    def during_hash(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        if not called:
            writer(env.db_path, sql, params)
            called = True
        return original(*args, **kwargs)

    monkeypatch.setattr(observation, "_hash", during_hash)
    result = inspect(env.db_path, env.scope)
    assert called and "sql_changed" in result.unresolved


@pytest.mark.parametrize("phase", ["first", "second", "fs_refusal"])
def test_sql_failure_and_fs_refusal_close_every_owned_handle(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    before, workers = fds(), threading.enumerate()
    connections, cursors, _ = trace_connections(monkeypatch)
    original = observation.collect_references
    calls = 0

    def capture(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == (1 if phase == "first" else 2) and phase != "fs_refusal":
            args[0].execute("SELECT * FROM file_claim_namespaces")
            raise sqlite3.OperationalError("simulated unavailable boundary")
        return original(*args, **kwargs)

    monkeypatch.setattr(observation, "collect_references", capture)
    if phase == "fs_refusal":
        marker = namespace / ".mangarr-claims" / ".namespace"
        # Use the actual producer marker role rather than guessing its filename.
        db = sqlite3.connect(ledger_path)
        try:
            closure = original(db, str(namespace), InventoryBudget())
        finally:
            db.close()
        marker = next(Path(r.path) for r in closure.roles if r.role.kind == "marker")
        marker.chmod(0o644)
    result = inspect(ledger_path, namespace)
    assert (
        "role_policy_mismatch" if phase == "fs_refusal" else "sql_unavailable"
    ) in result.unresolved
    assert_closed(connections, cursors)
    assert fds() == before and threading.enumerate() == workers


@pytest.mark.parametrize("size", [512 * 1024, 17 * 1024**2])
def test_sql_early_length_limit_and_ordinary_large_metadata(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
) -> None:
    db = sqlite3.connect(ledger_path)
    try:
        db.execute(
            "INSERT INTO root_folders(path,label) VALUES('/unrelated',?)", ("x" * size,)
        )
        db.commit()
    finally:
        db.close()
    original = observation._hash
    hashes = 0

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal hashes
        hashes += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(observation, "_hash", counted)
    result = inspect(ledger_path, namespace)
    if size > 16 * 1024**2:
        assert result.unresolved == ("evidence_budget_exceeded",)
        assert hashes == 0
    else:
        assert result.unresolved == () and hashes > 0


def test_sql_streamed_capture_budget_never_hashes_media(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = sqlite3.connect(ledger_path)
    try:
        db.executemany(
            "INSERT INTO root_folders(path,label) VALUES(?,?)",
            [(f"/unrelated/{i}", "x" * (512 * 1024)) for i in range(64)],
        )
        db.commit()
    finally:
        db.close()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("SQL budget failure reached media")

    monkeypatch.setattr(observation, "_observe_closure", forbidden)
    result = inspect(ledger_path, namespace)
    assert result.unresolved == ("evidence_budget_exceeded",)


def test_sql_media_budget_is_independent_and_zero_stays_invalid(
    ledger_path: Path,
    namespace: Path,
) -> None:
    result = inspect(ledger_path, namespace, InventoryBudget(max_hash_bytes=1))
    assert result.closure.snapshots
    assert "role_budget_exceeded" in result.unresolved
    with pytest.raises(InvalidIdentity):
        InventoryBudget(max_hash_bytes=0)


def test_sql_guard_and_inputs_keep_typed_refusals(ledger_path: Path) -> None:
    with file_mutation_guard(str(ledger_path)) as guard:
        with pytest.raises(InvalidIdentity):
            observation.inspect_ownership(guard, "relative", InventoryBudget())
        with pytest.raises(InvalidIdentity):
            observation.inspect_ownership(
                guard, str(ledger_path.parent), cast(InventoryBudget, object())
            )
    with pytest.raises(FileMutationLockError):
        observation.inspect_ownership(guard, str(ledger_path.parent), InventoryBudget())


def test_sql_actual_producer_closure_identity_and_wire(
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = producer_media
    producer_rescan(env, "prepared")
    closure, before_rows = captured_fixture(env)
    before_media = media_snapshot(env.scope)
    monkeypatch.setattr(
        observation,
        "capture_native",
        lambda fd: NativeHandle(1, f"simulated-real-{os.fstat(fd).st_ino}".encode()),
    )
    result = inspect(env.db_path, env.scope)
    assert result.closure == closure and not result.unresolved
    for record in result.observed:
        current = json.loads(record.constraints_json)["current"]
        if current is not None:
            info = Path(record.spec.path).stat()
            assert (current["dev"], current["inode"]) == (info.st_dev, info.st_ino)
    assert rows(env.db_path) == before_rows
    assert media_snapshot(env.scope) == before_media


def test_sql_escaped_row_is_bounded_before_adapter_serialization(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import file_identity_consumers as consumers

    db = sqlite3.connect(ledger_path)
    try:
        db.execute(
            "INSERT INTO root_folders(path,label) VALUES('/unrelated',?)",
            ("\x01" * (3 * 1024**2),),
        )
        db.commit()
    finally:
        db.close()
    original = consumers._encode

    def guarded(value: Any) -> str:
        if isinstance(value, tuple) and any(
            name == "label" and isinstance(text, str) and len(text) > 1024**2
            for name, text in value
        ):
            pytest.fail(
                "oversized escaped SQL row reached allocating adapter serializer"
            )
        return original(value)

    monkeypatch.setattr(consumers, "_encode", guarded)
    result = inspect(ledger_path, namespace)
    assert result.unresolved == ("evidence_budget_exceeded",)


def test_sql_programming_errors_are_not_operational_outcomes(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.ProgrammingError("programmer defect")

    monkeypatch.setattr(observation, "collect_references", broken)
    with pytest.raises(sqlite3.ProgrammingError, match="programmer defect"):
        inspect(ledger_path, ledger_path.parent)


def test_sql_whole_escaped_report_refuses_before_media(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = sqlite3.connect(ledger_path)
    try:
        db.execute(
            "UPDATE file_claim_namespaces SET ownership_json=ownership_json||?",
            ("\n" * (1536 * 1024),),
        )
        db.commit()
    finally:
        db.close()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unreportable escaped closure reached media hashing")

    monkeypatch.setattr(observation, "_hash", forbidden)
    result = inspect(ledger_path, namespace)
    assert result.unresolved == ("evidence_budget_exceeded",)
    assert result.observed == () and len(result.to_json()) <= 16 * 1024**2


@pytest.mark.parametrize("mode", ["absent", "unsupported"])
def test_sql_preserves_complete_current_absence_and_unsupported_native(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    db = sqlite3.connect(ledger_path)
    try:
        closure = observation.collect_references(db, str(namespace), InventoryBudget())
    finally:
        db.close()
    if mode == "absent":
        next(Path(r.path) for r in closure.roles if r.role.kind == "marker").unlink()
    else:

        def unsupported(fd: int) -> NativeHandle:
            raise IdentityUnavailable("explicitly simulated native-unsupported")

        monkeypatch.setattr(observation, "capture_native", unsupported)
    result = inspect(ledger_path, namespace)
    assert not result.unresolved
    assert ("absent" if mode == "absent" else "native_unsupported") in {
        r.outcome for r in result.observed
    }


def test_sql_real_second_capture_busy_closes_before_actor_shutdown(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections, cursors, _ = trace_connections(monkeypatch)
    original = observation._hash
    with ExitStack() as actors:
        called = False

        def during_hash(*args: Any, **kwargs: Any) -> Any:
            nonlocal called
            if not called:
                assert_closed(connections, cursors)
                actors.enter_context(
                    sql_actor(
                        ledger_path, ("PRAGMA journal_mode=DELETE", "BEGIN EXCLUSIVE")
                    )
                )
                called = True
            return original(*args, **kwargs)

        monkeypatch.setattr(observation, "_hash", during_hash)
        result = inspect(ledger_path, namespace)
        assert called and result.unresolved == ("sql_unavailable",)
        assert len(connections) == 2
        assert_closed(connections, cursors)


def test_sql_uri_is_percent_encoded_readonly_private_and_never_raw_opened(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.parse import quote

    renamed = ledger_path.with_name("identity ?#% \u00fc.db")
    ledger_path.rename(renamed)
    connect, opened = sqlite3.connect, os.open
    calls: list[str] = []

    def checked(database: Any, *args: Any, **kwargs: Any) -> Any:
        assert (
            database
            == "file:" + quote(str(renamed), safe="/") + "?mode=ro&cache=private"
        )
        assert kwargs["uri"] is True and kwargs["timeout"] == 0
        calls.append(database)
        return connect(database, *args, **kwargs)

    def no_raw(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert str(path) not in {
            str(renamed) + suffix for suffix in ("", "-wal", "-shm", "-journal")
        }
        return opened(path, flags, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", checked)
    monkeypatch.setattr(os, "open", no_raw)
    result = inspect(renamed, namespace)
    assert not result.unresolved and len(calls) == 2


@pytest.mark.parametrize(
    "budget", [InventoryBudget(max_rows=1), InventoryBudget(max_objects=1)]
)
def test_sql_capture_count_exhaustion_does_not_reach_media(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget: InventoryBudget,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("SQL count exhaustion reached media")

    monkeypatch.setattr(observation, "_observe_closure", forbidden)
    result = inspect(ledger_path, namespace, budget)
    assert result.unresolved == ("evidence_budget_exceeded",)


def test_sql_snapshot_envelope_exhaustion_is_incomplete_not_malformed(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = sqlite3.connect(ledger_path)
    try:
        text = db.execute(
            "SELECT ownership_json FROM file_claim_namespaces"
        ).fetchone()[0]
        raw = (("ownership_json", text), ("parent_path", str(namespace)))
        size = len(
            json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        )
        text += " " * (16 * 1024**2 - 1 - size)
        db.execute("UPDATE file_claim_namespaces SET ownership_json=?", (text,))
        db.commit()
    finally:
        db.close()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("snapshot envelope budget failure reached media")

    monkeypatch.setattr(observation, "_observe_closure", forbidden)
    result = inspect(ledger_path, namespace)
    assert result.unresolved == ("evidence_budget_exceeded",)


@pytest.mark.parametrize("changed_capture", [1, 2])
@pytest.mark.parametrize("seam", ["after_schema", "after_capture"])
@pytest.mark.parametrize(
    "change",
    ["PRAGMA user_version=7", "ALTER TABLE import_queue DROP COLUMN lease_owner"],
    ids=["version", "required_column"],
)
def test_sql_schema_freshness_spans_validation_and_capture(
    ledger_path: Path,
    namespace: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_capture: int,
    seam: str,
    change: str,
) -> None:
    before_fds, before_workers = fds(), threading.enumerate()
    connections, cursors, _ = trace_connections(monkeypatch)
    original_schema = observation._sql_schema
    original_capture = observation.collect_references
    original_observe = observation._observe_closure
    schema_calls = media_calls = writer_calls = 0

    def commit_change() -> None:
        nonlocal writer_calls
        writer(ledger_path, change)
        writer_calls += 1

    def validate(db: observation._ReadConnection) -> None:
        nonlocal schema_calls
        assert not db.in_transaction
        original_schema(db)
        schema_calls += 1
        if seam == "after_schema" and schema_calls == changed_capture:
            commit_change()

    def capture(
        db: sqlite3.Connection, parent: str, budget: InventoryBudget
    ) -> ReferenceClosure:
        assert not db.in_transaction
        closure = original_capture(db, parent, budget)
        assert not db.in_transaction
        if seam == "after_capture" and schema_calls == changed_capture:
            commit_change()
        return closure

    def observe(
        guard: FileMutationGuard,
        parent: str,
        closure: ReferenceClosure,
        budget: InventoryBudget,
    ) -> observation.OwnershipInventory:
        nonlocal media_calls
        assert_closed(connections, cursors)
        media_calls += 1
        return original_observe(guard, parent, closure, budget)

    monkeypatch.setattr(observation, "_sql_schema", validate)
    monkeypatch.setattr(observation, "collect_references", capture)
    monkeypatch.setattr(observation, "_observe_closure", observe)
    result = inspect(ledger_path, namespace)
    assert writer_calls == 1
    assert result.unresolved == ("sql_changed",)
    assert media_calls == int(changed_capture == 2)
    assert bool(result.observed) == (changed_capture == 2)
    assert len(connections) == changed_capture
    assert_closed(connections, cursors)
    db = sqlite3.connect(ledger_path)
    try:
        if change.startswith("PRAGMA"):
            assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        else:
            assert "lease_owner" not in {
                row[1] for row in db.execute("PRAGMA table_info(import_queue)")
            }
    finally:
        db.close()
    assert fds() == before_fds and threading.enumerate() == before_workers
