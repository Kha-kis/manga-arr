"""Offline contracts; none of these tests qualify a built SQLite binary."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from contextlib import nullcontext
from pathlib import Path
from multiprocessing.process import BaseProcess
from types import ModuleType
from typing import cast

import pytest
from scripts import sqlite_runtime

ROOT = Path(__file__).resolve().parents[2]


def archive(files: dict[str, bytes], *, trailer: bytes = b"") -> tuple[bytes, str]:
    manifest = b"C test\n" + b"".join(
        f"F {name} {hashlib.sha3_256(data).hexdigest()}\n".encode()
        for name, data in sorted(files.items())
    )
    identity = hashlib.sha3_256(manifest).hexdigest()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as package:
        for name, data in files.items():
            package.writestr("sqlite-src-3540100/" + name, data)
        package.writestr("sqlite-src-3540100/manifest", manifest + trailer)
        package.writestr("sqlite-src-3540100/manifest.uuid", identity + "\n")
    return stream.getvalue(), identity


def test_docker_build_contract() -> None:
    docker = (ROOT / "Dockerfile").read_text()
    base = "python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4"
    assert [
        line.split()[1] for line in docker.splitlines() if line.startswith("FROM ")
    ] == [base, base]
    assert "COPY --from=sqlite-builder /out/ /opt/mangarr/sqlite/3.54.1/" in docker
    assert "/etc/ld.so.conf.d/00-mangarr-sqlite.conf" in docker
    assert "ldconfig" in docker
    assert "LD_PRELOAD" not in docker and "LD_LIBRARY_PATH" not in docker
    final = docker.split("FROM ")[-1]
    assert (
        "build-essential" not in final
        and "curl" not in final.split("# Pinned Python")[0]
    )
    assert "sqlite_runtime.py verify" in final
    assert "!scripts/sqlite_runtime.py" in (ROOT / ".dockerignore").read_text()


def test_source_pins_and_vendor_defaults() -> None:
    module = sqlite_runtime
    assert module.SOURCE_URL == "https://www.sqlite.org/2026/sqlite-src-3540100.zip"
    assert (
        module.SOURCE_SHA3
        == "877c6f4996ce7adb368686bce21638de9e3d91534832ad3d9d119037d1bc22ad"
    )
    assert (
        module.SOURCE_ID
        == "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"
    )
    options = module.baseline()["compile_options"]
    assert len(options) == 60
    for option in (
        "MAX_FUNCTION_ARG=127",
        "MAX_VARIABLE_NUMBER=250000",
        "THREADSAFE=1",
        "DEFAULT_SYNCHRONOUS=2",
        "DEFAULT_WAL_SYNCHRONOUS=2",
        "DEFAULT_WAL_AUTOCHECKPOINT=1000",
    ):
        assert option in options
        assert "-DSQLITE_" + option in module.build_defines()
    assert "--soname=legacy" in module.CONFIGURE
    assert "--enable-update-limit" in module.CONFIGURE
    assert "--all" not in module.CONFIGURE
    assert "--disable-carray" in module.CONFIGURE
    assert "--disable-math" in module.CONFIGURE
    assert "-DSQLITE_ENABLE_MATH_FUNCTIONS" in module.build_defines()
    assert not any("COMPILER" in option for option in module.build_defines())


@pytest.mark.parametrize(
    "trailer", [b"", b"# Remove this line to create a well-formed Fossil manifest.\n"]
)
def test_verified_manifest(trailer: bytes) -> None:
    data, identity = archive(
        {"VERSION": b"3.54.1\n", "configure": b"#!/bin/sh\n"}, trailer=trailer
    )
    result = sqlite_runtime.verify_archive(
        data,
        expected_sha3=hashlib.sha3_256(data).hexdigest(),
        expected_identity=identity,
    )
    assert result["VERSION"] == b"3.54.1\n"
    assert result["configure"] == b"#!/bin/sh\n"


@pytest.mark.parametrize(
    "attack",
    [
        "hash",
        "identity",
        "modified",
        "unlisted",
        "missing",
        "traversal",
        "absolute",
        "symlink",
        "duplicate",
        "extra-trailer",
        "version",
    ],
)
def test_archive_refuses_tampering(attack: str) -> None:
    data, identity = archive({"VERSION": b"3.54.1\n", "configure": b"safe"})
    source = zipfile.ZipFile(io.BytesIO(data))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as target:
        for info in source.infolist():
            value = source.read(info)
            if attack == "missing" and info.filename.endswith("/configure"):
                continue
            if attack == "modified" and info.filename.endswith("/configure"):
                value = b"changed"
            if attack == "version" and info.filename.endswith("/VERSION"):
                value = b"3.54.0\n"
            if attack == "extra-trailer" and info.filename.endswith("/manifest"):
                value += b"# arbitrary normalization forbidden\n"
            target.writestr(info, value)
        extras = {
            "unlisted": "unlisted",
            "traversal": "../escape",
            "absolute": "/absolute",
            "duplicate": "configure",
            "symlink": "link",
        }
        if attack in extras:
            item = zipfile.ZipInfo("sqlite-src-3540100/" + extras[attack])
            if attack == "symlink":
                item.external_attr = 0o120777 << 16
            with pytest.warns(UserWarning) if attack == "duplicate" else nullcontext():
                target.writestr(item, b"bad")
    changed = stream.getvalue()
    digest = "0" * 64 if attack == "hash" else hashlib.sha3_256(changed).hexdigest()
    expected = "0" * 64 if attack == "identity" else identity
    with pytest.raises(ValueError):
        sqlite_runtime.verify_archive(
            changed, expected_sha3=digest, expected_identity=expected
        )


def test_option_reconciliation_is_closed() -> None:
    module = sqlite_runtime
    options = [
        o for o in module.baseline()["compile_options"] if not o.startswith("COMPILER=")
    ] + ["MAX_SCHEMA=10000000"]
    module.verify_options(options + ["COMPILER=gcc-15.2.0"])
    for bad in (
        options,
        options + ["COMPILER=a", "ENABLE_GEOPOLY"],
        [o for o in options if o != "ENABLE_SESSION"] + ["COMPILER=a"],
        options + ["COMPILER=a", "COMPILER=b"],
    ):
        with pytest.raises(ValueError):
            module.verify_options(bad)


def option_diagnostic(options: list[str]) -> dict[str, object]:
    with pytest.raises(ValueError) as caught:
        sqlite_runtime.verify_options(options)
    prefix, separator, detail = str(caught.value).partition(": ")
    assert prefix == "SQLite compile option drift" and separator == ": "
    return cast(dict[str, object], json.loads(detail))


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "unexpected",
        "duplicate",
        "no_compiler",
        "two_compilers",
        "duplicate_compiler",
        "empty_compiler",
    ],
)
def test_option_drift_retains_exact_difference(case: str) -> None:
    ordinary = [
        item
        for item in sqlite_runtime.baseline()["compile_options"]
        if not item.startswith("COMPILER=")
    ]
    ordinary.append("MAX_SCHEMA=10000000")
    options = ordinary + ["COMPILER=fixture"]
    missing: list[str] = []
    unexpected: list[str] = []
    duplicates: dict[str, int] = {}
    compilers = ["COMPILER=fixture"]
    if case == "missing":
        options.remove("ENABLE_SESSION")
        missing = ["ENABLE_SESSION"]
    elif case == "unexpected":
        options.append("ENABLE_GEOPOLY")
        unexpected = ["ENABLE_GEOPOLY"]
    elif case == "duplicate":
        options.extend(["ENABLE_SESSION", "ENABLE_SESSION"])
        duplicates = {"ENABLE_SESSION": 3}
    elif case == "no_compiler":
        options = ordinary
        compilers = []
    elif case == "two_compilers":
        options.append("COMPILER=other")
        compilers.append("COMPILER=other")
    elif case == "duplicate_compiler":
        options.append("COMPILER=fixture")
        compilers.append("COMPILER=fixture")
        duplicates = {"COMPILER=fixture": 2}
    else:
        options[-1] = "COMPILER="
        compilers = ["COMPILER="]
    assert option_diagnostic(options) == {
        "missing": missing,
        "unexpected": unexpected,
        "duplicates": duplicates,
        "compiler_count": len(compilers),
        "compilers": compilers,
    }


@pytest.mark.parametrize(
    "options,reason",
    [
        (None, "options_type"),
        ({"PRIVATE_CANARY": "not options"}, "options_type"),
        ([None], "option_type"),
        ([b"PRIVATE_CANARY"], "option_type"),
        (["X"] * 257, "option_count"),
        (["PRIVATE_CANARY" * 22], "option_length"),
        ([""], "option_length"),
        (["X\nPRIVATE_CANARY"], "option_characters"),
        (["X\x1bPRIVATE_CANARY"], "option_characters"),
        (["X\u2028PRIVATE_CANARY"], "option_characters"),
    ],
)
def test_option_diagnostic_refuses_malformed_without_echo(
    options: object, reason: str
) -> None:
    diagnostic = option_diagnostic(cast(list[str], options))
    assert diagnostic == {"malformed": reason}
    assert "PRIVATE_CANARY" not in json.dumps(diagnostic)


def test_option_diagnostic_is_complete_at_finite_bound() -> None:
    unexpected = [f"X{number:03d}_" + "A" * 251 for number in range(255)]
    compiler = "COMPILER=" + "c" * 247
    assert len(compiler) == 256 and all(len(item) == 256 for item in unexpected)
    diagnostic = option_diagnostic(unexpected + [compiler])
    assert diagnostic == {
        "missing": sorted(
            [
                item
                for item in sqlite_runtime.baseline()["compile_options"]
                if not item.startswith("COMPILER=")
            ]
            + ["MAX_SCHEMA=10000000"]
        ),
        "unexpected": unexpected,
        "duplicates": {},
        "compiler_count": 1,
        "compilers": [compiler],
    }
    assert len(json.dumps(diagnostic).encode()) < 256 * 1024


def test_actual_build3_option_shape_is_exactly_reconciled() -> None:
    options = list(sqlite_runtime.baseline()["compile_options"])
    options.append("MAX_SCHEMA=10000000")
    assert len(options) == 61
    before = list(options)
    assert sqlite_runtime.verify_options(options) is None
    assert options == before
    assert hashlib.sha256(
        (ROOT / "runtime/sqlite-baseline.json").read_bytes()
    ).hexdigest() == (
        "85d1a3a996c60d68b7950ad52e9b9ef1b0f0c4c85a031be707d09dc64a4d94db"
    )
    assert len(sqlite_runtime.baseline()["features"]) == 17
    assert not any(
        item.startswith("-DSQLITE_MAX_SCHEMA=")
        for item in sqlite_runtime.build_defines()
    )


@pytest.mark.parametrize("binding", ["VERSION", "SOURCE_ID"])
def test_schema_option_requires_exact_source_binding(
    monkeypatch: pytest.MonkeyPatch, binding: str
) -> None:
    monkeypatch.setattr(sqlite_runtime, binding, "unreviewed")
    with pytest.raises(ValueError, match="unreconciled SQLite source"):
        sqlite_runtime.verify_options(
            sqlite_runtime.baseline()["compile_options"] + ["MAX_SCHEMA=10000000"]
        )


@pytest.mark.parametrize(
    "addition",
    [
        [],
        ["MAX_SCHEMA=9999999"],
        ["MAX_SCHEMA=10000001"],
        ["MAX_SCHEMA=010000000"],
        ["MAX_SCHEMA=10000000"] * 2,
        ["MAX_SCHEMA=10000000", "MAX_SCHEMA_RETRY=50"],
        ["MAX_SCHEMA=10000000", "UNREVIEWED_OPTION"],
    ],
)
def test_schema_option_reconciliation_has_no_waiver(addition: list[str]) -> None:
    with pytest.raises(ValueError, match="SQLite compile option drift"):
        sqlite_runtime.verify_options(
            sqlite_runtime.baseline()["compile_options"] + addition
        )


def test_schema_reconciliation_preserves_every_vendor_option() -> None:
    original = sqlite_runtime.baseline()["compile_options"]
    for item in original:
        if item.startswith("COMPILER="):
            continue
        options = [value for value in original if value != item] + [
            "MAX_SCHEMA=10000000"
        ]
        assert option_diagnostic(options)["missing"] == [item]
        assert option_diagnostic(options + [item + "_DRIFT"])["unexpected"] == [
            item + "_DRIFT"
        ]


SCHEMA_DIAGNOSTIC_CASES: dict[str, tuple[object, object, str, str | None]] = {
    "diagnostic_error": (1, "SQLITE_ERROR", "too many schema objects", None),
    "diagnostic_escape": (4, "SQLITE_ABORT", 'other "quoted" failure \\', None),
    "diagnostic_bound": (4, "\\" * 64, '"' * 512, None),
    "diagnostic_code_none": (None, "SQLITE_ABORT", "PRIVATE_CANARY", "code_type"),
    "diagnostic_code_bool": (False, "SQLITE_ABORT", "PRIVATE_CANARY", "code_type"),
    "diagnostic_code_negative": (-1, "SQLITE_ABORT", "PRIVATE_CANARY", "code_range"),
    "diagnostic_code_huge": (2**31, "SQLITE_ABORT", "PRIVATE_CANARY", "code_range"),
    "diagnostic_name_none": (4, None, "PRIVATE_CANARY", "name_type"),
    "diagnostic_name_empty": (4, "", "PRIVATE_CANARY", "name_length"),
    "diagnostic_name_long": (4, "N" * 65, "PRIVATE_CANARY", "name_length"),
    "diagnostic_name_control": (4, "N\nPRIVATE_CANARY", "fixture", "name_characters"),
    "diagnostic_name_nonascii": (
        4,
        "N\u2028PRIVATE_CANARY",
        "fixture",
        "name_characters",
    ),
    "diagnostic_message_empty": (4, "SQLITE_ABORT", "", "message_length"),
    "diagnostic_message_long": (
        4,
        "SQLITE_ABORT",
        "PRIVATE_CANARY" * 40,
        "message_length",
    ),
    "diagnostic_message_control": (
        4,
        "SQLITE_ABORT",
        "M\nPRIVATE_CANARY",
        "message_characters",
    ),
    "diagnostic_message_nonascii": (
        4,
        "SQLITE_ABORT",
        "M\u2028PRIVATE_CANARY",
        "message_characters",
    ),
    "diagnostic_unavailable": (4, "SQLITE_ABORT", "fixture", "unavailable"),
    "diagnostic_error_unavailable": (1, "SQLITE_ERROR", "fixture", "unavailable"),
    "diagnostic_getter_unavailable": (4, "SQLITE_ABORT", "fixture", "unavailable"),
    "diagnostic_single_read": (4, "SQLITE_ABORT", "fixture", None),
}


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "default",
        "set",
        "set_query",
        "create",
        "no_overflow",
        "overflow_code",
        "overflow_message",
        "restore",
        "restore_query",
        "fresh",
        "close",
    ]
    + list(SCHEMA_DIAGNOSTIC_CASES),
)
def test_private_schema_probe_restores_closes_and_refuses_faults(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    class DatabaseError(Exception):
        def __init__(self, message: str, code: object = 4) -> None:
            super().__init__(message)
            self._code: object = code
            self.code_reads: int = 0
            self.message_reads: int = 0
            self.sqlite_errorname: object = (
                "SQLITE_ABORT"
                if code == 4
                else "SQLITE_ERROR"
                if code == 1
                else "SQLITE_INTERNAL"
            )

        @property
        def sqlite_errorcode(self) -> object:
            self.code_reads += 1
            if fault == "diagnostic_getter_unavailable" or (
                fault == "diagnostic_single_read" and self.code_reads > 1
            ):
                raise OSError("PRIVATE_CANARY")
            return self._code

        def __str__(self) -> str:
            self.message_reads += 1
            if fault in ("diagnostic_unavailable", "diagnostic_error_unavailable") or (
                fault == "diagnostic_single_read" and self.message_reads > 1
            ):
                raise OSError("PRIVATE_CANARY")
            return super().__str__()

    observed_errors: list[DatabaseError] = []

    class Connection:
        def __init__(self, number: int) -> None:
            self.number = number
            self.limit = (
                9000000
                if fault == "default" or fault == "fresh" and number == 1
                else 10000000
            )
            self.sets: list[int] = []
            self.sql: list[str] = []
            self.objects = 1
            self.closed = False

        def getlimit(self, category: int) -> int:
            assert category == 13
            if fault == "set_query" and self.limit == 5:
                return 6
            if fault == "restore_query" and self.sets == [5, 10000000]:
                return 5
            return self.limit

        def setlimit(self, category: int, value: int) -> int:
            assert category == 13
            self.sets.append(value)
            if (
                value == 5
                and fault == "set"
                or value == 10000000
                and fault == "restore"
            ):
                raise OSError("controlled limit failure")
            old, self.limit = self.limit, value
            return old

        def execute(self, sql: str) -> None:
            self.sql.append(sql)
            if fault == "create" and len(self.sql) == 2:
                raise OSError("controlled fixture failure")
            expected = [
                "CREATE TABLE t1(a,b,c UNIQUE)",
                "CREATE INDEX x1 ON t1(a)",
                "CREATE VIEW v1 AS SELECT a+b AS x FROM t1",
                "CREATE TRIGGER r1 AFTER INSERT ON t1 BEGIN SELECT true; END",
            ]
            assert sql == expected[len(self.sql) - 1]
            self.objects += 2 if len(self.sql) == 1 else 1
            if self.objects > self.limit and fault != "no_overflow":
                if fault in SCHEMA_DIAGNOSTIC_CASES:
                    code, name, message, _ = SCHEMA_DIAGNOSTIC_CASES[fault]
                    error = DatabaseError(message, code)
                    error.sqlite_errorname = name
                else:
                    error = DatabaseError(
                        "wrong error"
                        if fault == "overflow_message"
                        else "too many schema objects",
                        2 if fault == "overflow_code" else 4,
                    )
                observed_errors.append(error)
                raise error

        def close(self) -> None:
            self.closed = True
            if fault == "close":
                raise OSError("controlled close failure")

    connections: list[Connection] = []

    def connect(database: str) -> Connection:
        assert database == ":memory:"
        connection = Connection(len(connections))
        connections.append(connection)
        return connection

    fake = ModuleType("sqlite3")
    monkeypatch.setattr(fake, "connect", connect, raising=False)
    monkeypatch.setattr(fake, "Error", DatabaseError, raising=False)
    monkeypatch.setattr(fake, "SQLITE_ERROR", 1, raising=False)
    monkeypatch.setattr(fake, "SQLITE_ABORT", 4, raising=False)
    monkeypatch.setitem(sys.modules, "sqlite3", fake)
    probe = sqlite_runtime._verify_schema_limit
    if fault == "none":
        assert probe() == {
            "default": 10000000,
            "fixture_limit": 5,
            "fresh_connection": 10000000,
        }
        assert len(connections) == 2 and connections[0].objects == 6
    else:
        with pytest.raises((ValueError, OSError)) as caught:
            probe()
        if fault in SCHEMA_DIAGNOSTIC_CASES:
            code, name, message, malformed = SCHEMA_DIAGNOSTIC_CASES[fault]
            prefix, separator, detail = str(caught.value).partition(": ")
            assert (
                prefix == "SQLite schema overflow error mismatch" and separator == ": "
            )
            assert json.loads(detail) == (
                {"malformed": malformed}
                if malformed is not None
                else {
                    "sqlite_errorcode": code,
                    "sqlite_errorname": name,
                    "message": message,
                }
            )
            assert len(str(caught.value).encode()) < 2048
            if malformed is not None:
                assert "PRIVATE_CANARY" not in str(caught.value)
    assert connections and all(connection.closed for connection in connections)
    if fault != "default":
        assert connections[0].sets[-1] == 10000000
    if len(connections) == 2:
        assert connections[1].sets == []
    for error in observed_errors:
        assert error.code_reads == 1
        assert error.message_reads == (
            0 if fault == "diagnostic_getter_unavailable" else 1
        )


def test_runtime_observation_requires_schema_probe() -> None:
    import ast

    module = ast.parse((ROOT / "scripts/sqlite_runtime.py").read_text())
    verify = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "verify_runtime"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_verify_schema_limit"
        for node in ast.walk(verify)
    )


def test_provenance_hash_and_root_ownership(tmp_path: Path) -> None:
    module = sqlite_runtime
    library = tmp_path / "libsqlite3.so.0"
    library.write_bytes(b"fixture ELF")
    library.chmod(0o644)
    expected = hashlib.sha256(library.read_bytes()).hexdigest()
    module.verify_artifact(library, expected, require_root=False)
    with pytest.raises(ValueError):
        module.verify_artifact(library, "0" * 64, require_root=False)
    library.chmod(0o666)
    with pytest.raises(ValueError):
        module.verify_artifact(library, expected, require_root=False)
    library.chmod(0o644)
    alias = tmp_path / "alias"
    alias.symlink_to(library)
    with pytest.raises(ValueError):
        module.verify_artifact(alias, expected, require_root=False)


def test_maps_requires_unique_private_library() -> None:
    module = sqlite_runtime
    private = "/opt/mangarr/sqlite/3.54.1/lib/libsqlite3.so.0"
    assert module.mapped_sqlite_paths(f"1-2 r-xp 0000 00:01 1 {private}\n") == {private}
    assert module.mapped_sqlite_paths(
        "1-2 r-xp 0 0:0 1 /usr/lib/libsqlite3.so.0 (deleted)\n"
    ) != {private}


def test_baseline_record_is_public_and_exact() -> None:
    baseline = sqlite_runtime.baseline()
    assert baseline["package_version"] == "3.46.1-7+deb13u1"
    assert (
        baseline["extension"]["sha256"]
        == "104cfbbb2f3e56860c6b76543036c2098d47598ae642ea8a4fc7ddda8c700b9a"
    )
    assert (
        baseline["library"]["sha256"]
        == "14c4418a06c5c2e30f5fb57bc8add93762236ae17898c6708984cf15e680ca71"
    )
    assert len(baseline["features"]) == 17
    assert all(baseline["features"].values())


def test_wal_qualification_cannot_write_before_runtime_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> dict[str, object]:
        raise ValueError("not the private runtime")

    monkeypatch.setattr(sqlite_runtime, "verify_runtime", refuse)
    workspace = tmp_path / "never-created"
    with pytest.raises(ValueError, match="not the private runtime"):
        sqlite_runtime.qualify_wal(workspace)
    assert not workspace.exists()


def test_wal_qualification_refuses_existing_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite_runtime, "verify_runtime", lambda: {"status": "fixture"})
    marker = tmp_path / "preserved"
    marker.write_bytes(b"do not replace")
    with pytest.raises(FileExistsError):
        sqlite_runtime.qualify_wal(tmp_path)
    assert marker.read_bytes() == b"do not replace"


def test_download_refuses_redirect() -> None:
    from urllib.request import Request

    with pytest.raises(ValueError, match="redirect"):
        sqlite_runtime._NoRedirect().redirect_request(
            Request(sqlite_runtime.SOURCE_URL),
            None,
            302,
            "redirect",
            None,
            "https://example.invalid/",
        )


def test_builder_does_not_execute_unverified_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(sqlite_runtime, "download_source", lambda: b"not authenticated")
    calls: list[list[str]] = []

    def command(argv: list[str], *, cwd: Path | None = None) -> str:
        assert cwd is not None
        calls.append(argv)
        raise AssertionError("no command may run")

    monkeypatch.setattr(sqlite_runtime, "_run", command)
    with pytest.raises(ValueError, match="SHA3"):
        sqlite_runtime.build(tmp_path / "out", tmp_path / "work")
    assert calls == []


@pytest.mark.parametrize(
    "tamper",
    [
        "none",
        "missing_export",
        "modified_source",
        "no_relro",
        "no_now",
        "unrelated_now",
        "default_rpath",
        "reenabled_rpath",
        "library_rpath",
        "library_runpath",
        "extension_rpath",
        "extension_runpath",
    ],
)
def test_builder_binds_generated_sources_and_elf_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    import hashlib

    extension = tmp_path / "_sqlite3.so"
    _ = extension.write_bytes(b"CPython extension fixture")
    fake_extension = ModuleType("_sqlite3")
    fake_extension.__file__ = str(extension)
    monkeypatch.setitem(sys.modules, "_sqlite3", fake_extension)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(sqlite_runtime, "download_source", lambda: b"archive fixture")
    authenticated: list[bool] = []

    def authenticate(data: bytes) -> dict[str, bytes]:
        assert data == b"archive fixture"
        authenticated.append(True)
        return {"configure": b"#!/bin/sh\n", "src/parse.y": b"grammar fixture"}

    monkeypatch.setattr(sqlite_runtime, "verify_archive", authenticate)
    options = [
        option
        for option in sqlite_runtime.baseline()["compile_options"]
        if not option.startswith("COMPILER=")
    ] + ["COMPILER=fixture", "MAX_SCHEMA=10000000"]
    calls: list[list[str]] = []
    library_rpath = ""
    if tamper == "default_rpath":
        monkeypatch.setattr(
            sqlite_runtime,
            "CONFIGURE",
            tuple(arg for arg in sqlite_runtime.CONFIGURE if arg != "--disable-rpath"),
        )
    elif tamper == "reenabled_rpath":
        monkeypatch.setattr(
            sqlite_runtime, "CONFIGURE", (*sqlite_runtime.CONFIGURE, "--enable-rpath")
        )

    def command(argv: list[str], *, cwd: Path | None = None) -> str:
        nonlocal library_rpath
        assert authenticated == [True]
        calls.append(argv)
        if argv[0].endswith("/configure"):
            assert "--soname=legacy" in argv and "--enable-update-limit" in argv
            assert cwd is not None
            # Pinned sqlite-handle-rpath -> Makefile.in LDFLAGS.rpath.
            enabled = True
            for arg in argv[1:]:
                if arg in ("--enable-rpath", "--disable-rpath"):
                    enabled = arg == "--enable-rpath"
            value = "-Wl,-rpath -Wl,/usr/local/lib" if enabled else ""
            _ = (cwd / "Makefile").write_text("LDFLAGS.rpath = " + value + "\n")
        elif argv[0] == "make":
            assert cwd is not None
            # main.mk LDFLAGS.libsqlite3 consumes the configured variable.
            library_rpath = (
                (cwd / "Makefile").read_text().removeprefix("LDFLAGS.rpath = ").strip()
            )
            assert "LDFLAGS.math=-lm" in argv
            assert any(
                item.startswith("OPTS=")
                and "-DSQLITE_ENABLE_UPDATE_DELETE_LIMIT" in item
                and "-DSQLITE_MAX_FUNCTION_ARG=127" in item
                for item in argv
            )
            for name in (
                "parse.c",
                "parse.h",
                "sqlite3.c",
                "sqlite3.h",
                "config.log",
                "libsqlite3.so",
            ):
                _ = (cwd / name).write_bytes(name.encode())
            if tamper == "modified_source":
                _ = (cwd.parent / "source/src/parse.y").write_bytes(
                    b"changed after authentication"
                )
        elif argv[0] == "readelf":
            if "-lW" in argv:
                return "GNU_STACK" if tamper == "no_relro" else "  GNU_RELRO 0x1000\n"
            is_library = Path(argv[-1]).name == "libsqlite3.so"
            loader = (
                "(RUNPATH) Library runpath: [/usr/local/lib]\n"
                if is_library
                and (library_rpath or tamper == "library_runpath")
                or not is_library
                and tamper == "extension_runpath"
                else "(RPATH) Library rpath: [/usr/local/lib]\n"
                if is_library
                and tamper == "library_rpath"
                or not is_library
                and tamper == "extension_rpath"
                else ""
            )
            flags = (
                ""
                if tamper == "no_now"
                else "(FLAGS_1) Flags: NOWARN"
                if tamper == "unrelated_now"
                else "(FLAGS) BIND_NOW"
            )
            return (
                (
                    "Library soname: [libsqlite3.so.0]\nShared library: [libsqlite3.so.0]\n"
                )
                + flags
                + "\n"
                + loader
            )
        elif argv[0] == "nm":
            if "--undefined-only" in argv:
                return "U sqlite3_open\n"
            names = list(sqlite_runtime.REQUIRED_EXPORTS) + (
                [] if tamper == "missing_export" else ["sqlite3_open"]
            )
            return "\n".join("0000 T " + name for name in names)
        elif argv[0] == sys.executable:
            return json.dumps(
                {"source_id": sqlite_runtime.SOURCE_ID, "options": options}
            )
        return "fixture toolchain"

    monkeypatch.setattr(sqlite_runtime, "_run", command)
    output, work = tmp_path / "out", tmp_path / "work"
    if tamper != "none":
        with pytest.raises(
            ValueError,
            match="exports"
            if tamper == "missing_export"
            else "canonical source changed"
            if tamper == "modified_source"
            else "unexpected ELF loader path"
            if "rpath" in tamper or "runpath" in tamper
            else "ELF RELRO/BIND_NOW",
        ):
            sqlite_runtime.build(output, work)
        assert not (output / "provenance.json").exists()
    else:
        sqlite_runtime.build(output, work)
        provenance = cast(
            dict[str, object], json.loads((output / "provenance.json").read_text())
        )
        assert (
            provenance["library_sha256"] == hashlib.sha256(b"libsqlite3.so").hexdigest()
        )
        assert (
            provenance["extension_sha256"]
            == hashlib.sha256(extension.read_bytes()).hexdigest()
        )
        assert provenance["compile_options"] == options
        assert (work / "generated/Makefile").read_text() == "LDFLAGS.rpath = \n"
        assert library_rpath == ""
        assert set(cast(dict[str, str], provenance["generated_sha256"])) == {
            "parse.c",
            "parse.h",
            "sqlite3.c",
            "sqlite3.h",
            "Makefile",
            "config.log",
        }
        assert (output / "sqlite_runtime.py").is_file()
        assert (output / "sqlite-baseline.json").is_file()


@pytest.mark.parametrize("machine,expected", [(62, "x86_64"), (183, "aarch64")])
def test_actual_elf_header_machine(tmp_path: Path, machine: int, expected: str) -> None:
    artifact = tmp_path / "ELF"
    _ = artifact.write_bytes(
        b"\x7fELF\x02\x01\x01" + bytes(11) + machine.to_bytes(2, "little")
    )
    assert sqlite_runtime.elf_machine(artifact) == expected


@pytest.mark.parametrize(
    "header",
    [
        b"",
        b"not ELF",
        b"\x7fELF\x01\x01\x01" + bytes(13),
        b"\x7fELF\x02\x02\x01" + bytes(13),
        b"\x7fELF\x02\x01\x01" + bytes(13),
    ],
)
def test_refuses_wrong_or_unknown_elf(tmp_path: Path, header: bytes) -> None:
    artifact = tmp_path / "ELF"
    _ = artifact.write_bytes(header)
    with pytest.raises(ValueError, match="ELF"):
        sqlite_runtime.elf_machine(artifact)


def test_default_probe_distinguishes_memory_from_disk_autovacuum() -> None:
    assert sqlite_runtime.MEMORY_DEFAULTS == {
        "synchronous": 2,
        "wal_autocheckpoint": 1000,
        "page_size": 4096,
        "foreign_keys": 0,
        "recursive_triggers": 1,
        "auto_vacuum": 0,
        "temp_store": 0,
    }
    assert "DEFAULT_AUTOVACUUM" in sqlite_runtime.baseline()["compile_options"]


@pytest.mark.parametrize(
    "status,alive,stubborn,unreapable",
    [
        (0, False, False, False),
        (2, False, False, False),
        (None, True, False, False),
        (None, True, True, False),
        (None, True, True, True),
    ],
)
def test_wal_worker_terminal_and_failure_cleanup(
    status: int | None, alive: bool, stubborn: bool, unreapable: bool
) -> None:
    calls: list[str] = []

    class Worker(BaseProcess):
        def __init__(self) -> None:
            super().__init__()
            self.alive: bool = alive
            self.result: int | None = status

        @property
        def pid(self) -> int:
            return 12345

        @property
        def exitcode(self) -> int | None:
            return self.result

        def join(self, timeout: float | None = None) -> None:
            calls.append("join:" + str(timeout))

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            calls.append("terminate")
            self.alive = stubborn
            if not self.alive:
                self.result = -15

        def kill(self) -> None:
            calls.append("kill")
            self.alive = unreapable
            if not self.alive:
                self.result = -9

        def close(self) -> None:
            assert not self.alive
            calls.append("close")

    worker = Worker()
    if status == 0:
        sqlite_runtime._join_workers([worker])
    elif unreapable:
        with pytest.raises(ValueError, match="cleanup incomplete"):
            sqlite_runtime._join_workers([worker])
        assert calls[-1] == "join:5" and "close" not in calls
        return
    else:
        with pytest.raises(ValueError, match="failed or exceeded"):
            sqlite_runtime._join_workers([worker])
    assert calls[0] == "join:60" and calls[-1] == "close"
    assert ("terminate" in calls) == alive
    assert ("kill" in calls) == stubborn


def test_builder_refuses_nonroot_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(ValueError, match="root"):
        sqlite_runtime.build(tmp_path / "out", tmp_path / "work")
    assert list(tmp_path.iterdir()) == []


def test_existing_fast_gate_includes_offline_runtime_contracts() -> None:
    makefile = (ROOT / "Makefile").read_text()
    fast = makefile.split("test-fast: lint\n", 1)[1].split("test-python:", 1)[0]
    assert "tests/python/test_sqlite_runtime.py" in fast
    assert "tests/python/test_other_uid_actor_runtime.py" in fast
    lint = makefile.split("lint:\n", 1)[1].split("test-fast:", 1)[0]
    assert "scripts/sqlite_runtime.py" in lint


def test_provenance_type_admission_does_not_open_special_files(tmp_path: Path) -> None:
    fifo = tmp_path / "provenance.json"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match="ownership/type/mode"):
        sqlite_runtime._verify_private_file(fifo, require_root=False)


class ControlledWorker(BaseProcess):
    """No process is spawned; method faults exercise exact-handle ownership."""

    def __init__(
        self,
        role: str,
        *,
        fail_start: str | None = None,
        unknown_start: bool = False,
        fault: str | None = None,
        linger: bool = False,
    ) -> None:
        super().__init__()
        self.role: str = role
        self.fail_start: str | None = fail_start
        self.unknown_start: bool = unknown_start
        self.fault: str | None = fault
        self.linger: bool = linger
        self.child_pid: int | None = None
        self.status: int | None = None
        self.alive: bool = False
        self.closed: bool = False
        self.calls: list[str] = []

    @property
    def pid(self) -> int | None:
        return self.child_pid

    @property
    def exitcode(self) -> int | None:
        return self.status

    def start(self) -> None:
        self.calls.append("start")
        self.child_pid = None if self.unknown_start else 12345
        self.alive = True
        if self.role == self.fail_start:
            raise InterruptedError("injected partial start")

    def join(self, timeout: float | None = None) -> None:
        self.calls.append("join:" + str(timeout))
        if self.fault == "join":
            raise PermissionError("injected join failure")
        if timeout == 60 and not self.linger:
            self.alive = False
            self.status = 0

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.calls.append("terminate")
        if self.fault == "terminate":
            raise PermissionError("injected terminate failure")
        if self.fault != "kill":
            self.alive = False
            self.status = -15

    def kill(self) -> None:
        self.calls.append("kill")
        if self.fault == "kill":
            raise PermissionError("injected kill failure")
        self.alive = False
        self.status = -9

    def close(self) -> None:
        self.calls.append("close")
        if self.fault == "close":
            raise PermissionError("injected close failure")
        assert not self.alive
        self.closed = True


def inject_wal_workers(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    *,
    unknown: bool = False,
) -> list[ControlledWorker]:
    created: list[ControlledWorker] = []

    class Row:
        def __init__(self, value: tuple[object, ...]) -> None:
            self.value: tuple[object, ...] = value

        def fetchone(self) -> tuple[object, ...]:
            return self.value

    class Connection:
        def execute(self, sql: str) -> Row:
            return Row(
                {
                    "PRAGMA auto_vacuum": (1,),
                    "PRAGMA journal_mode=WAL": ("wal",),
                    "PRAGMA synchronous": (2,),
                    "PRAGMA locking_mode": ("normal",),
                }.get(sql, ())
            )

        def commit(self) -> None:
            pass

        def close(self) -> None:
            pass

    class Ready:
        def set(self) -> None:
            pass

    class Context:
        def Event(self) -> Ready:
            return Ready()

        def Process(
            self, *, target: object, args: tuple[str, str, object]
        ) -> ControlledWorker:
            assert callable(target)
            worker = ControlledWorker(args[1], fail_start=role, unknown_start=unknown)
            created.append(worker)
            return worker

    fake_sqlite = ModuleType("sqlite3")
    monkeypatch.setattr(fake_sqlite, "connect", lambda _: Connection(), raising=False)
    monkeypatch.setitem(sys.modules, "sqlite3", fake_sqlite)
    monkeypatch.setattr(sqlite_runtime, "verify_runtime", lambda: {"injected": True})
    monkeypatch.setattr(
        sqlite_runtime.multiprocessing, "get_context", lambda _: Context()
    )
    return created


@pytest.mark.parametrize("role", ["writer", "checkpoint", "crash"])
def test_start_exception_reconciles_each_identified_pending_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    created = inject_wal_workers(monkeypatch, role)
    with pytest.raises(InterruptedError, match="partial start"):
        sqlite_runtime.qualify_wal(tmp_path / "owned")
    assert all(worker.closed and not worker.alive for worker in created)


def test_unknown_partial_start_never_claims_reap_or_signals_unknown_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = inject_wal_workers(monkeypatch, "writer", unknown=True)
    with pytest.raises(ValueError, match="cleanup incomplete"):
        sqlite_runtime.qualify_wal(tmp_path / "owned")
    assert len(created) == 1 and created[0].pid is None
    assert created[0].calls == ["start"] and not created[0].closed


def test_genuinely_unstarted_pending_handle_is_not_joined_or_signalled() -> None:
    worker = ControlledWorker("unstarted")
    sqlite_runtime._join_workers([worker], start_attempted=set())
    assert worker.closed and worker.calls == ["close"]


@pytest.mark.parametrize("method", ["join", "terminate", "kill", "close"])
def test_cleanup_method_error_does_not_skip_later_known_workers(method: str) -> None:
    first = ControlledWorker("first", fault=method, linger=True)
    second = ControlledWorker("second", linger=True)
    first.start()
    second.start()
    with pytest.raises(ValueError, match="cleanup incomplete"):
        sqlite_runtime._join_workers([first, second])
    assert second.closed and not second.alive
    assert "terminate" in second.calls and "close" in second.calls
    assert all(
        call != "join:None" for worker in (first, second) for call in worker.calls
    )


def test_normal_identified_worker_cleanup_control() -> None:
    worker = ControlledWorker("normal")
    worker.start()
    sqlite_runtime._join_workers([worker])
    assert worker.closed and not worker.alive and worker.exitcode == 0


def test_link_hardening_is_a_supported_configure_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DispatchCaptured(Exception):
        pass

    calls: list[list[str]] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(sqlite_runtime, "download_source", lambda: b"fixture")
    monkeypatch.setattr(
        sqlite_runtime, "verify_archive", lambda _: {"configure": b"#!/bin/sh\n"}
    )

    def command(argv: list[str], *, cwd: Path | None = None) -> str:
        assert cwd is not None
        calls.append(argv)
        if argv[0] == "make":
            raise DispatchCaptured
        return ""

    monkeypatch.setattr(sqlite_runtime, "_run", command)
    with pytest.raises(DispatchCaptured):
        sqlite_runtime.build(tmp_path / "out", tmp_path / "work")
    assert "LDFLAGS=-Wl,-z,relro,-z,now" in calls[0]
    assert not any(argument.startswith("LDFLAGS=") for argument in calls[1])
