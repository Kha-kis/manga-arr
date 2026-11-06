#!/usr/bin/env python3
"""Build/verify Mangarr's private SQLite. Build mode runs only in its builder stage."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import multiprocessing
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from collections import Counter
from contextlib import closing
from http.client import HTTPResponse
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Event
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast

VERSION = "3.54.1"
BASE = "python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4"
SOURCE_URL = "https://www.sqlite.org/2026/sqlite-src-3540100.zip"
SOURCE_SHA3 = "877c6f4996ce7adb368686bce21638de9e3d91534832ad3d9d119037d1bc22ad"
SOURCE_ID = "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"
SOURCE_UUID = "555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"
PREFIX = Path("/opt/mangarr/sqlite/3.54.1")
MAX_ARCHIVE = 24 * 1024 * 1024
MAX_SOURCE = 160 * 1024 * 1024
CONFIGURE = (
    "--soname=legacy",
    "--disable-rpath",
    "--enable-shared",
    "--disable-static",
    "--enable-threadsafe",
    "--with-tempstore=no",
    "--enable-load-extension",
    "--enable-update-limit",
    "--disable-carray",
    "--disable-math",
    "--disable-readline",
)
AUTO_OPTIONS = {"ATOMIC_INTRINSICS=1", "MUTEX_PTHREADS", "SYSTEM_MALLOC", "HAVE_ISNAN"}
REQUIRED_EXPORTS = (
    "sqlite3session_create",
    "sqlite3_preupdate_hook",
    "sqlite3_unlock_notify",
    "sqlite3_column_table_name",
    "sqlite3_backup_init",
    "sqlite3_serialize",
    "sqlite3_deserialize",
    "sqlite3_load_extension",
    "sqlite3_enable_load_extension",
)
MEMORY_DEFAULTS = {
    "synchronous": 2,
    "wal_autocheckpoint": 1000,
    "page_size": 4096,
    "foreign_keys": 0,
    "recursive_triggers": 1,
    "auto_vacuum": 0,
    "temp_store": 0,
}


class ArtifactHash(TypedDict):
    path: str
    sha256: str


class Baseline(TypedDict):
    compile_options: list[str]
    features: dict[str, bool]
    package_version: str
    extension: ArtifactHash
    library: ArtifactHash


class Provenance(TypedDict):
    schema: int
    base: str
    version: str
    source_id: str
    source_sha3_256: str
    library_sha256: str
    extension_sha256: str
    machine: str
    compile_options: list[str]
    build_script_sha256: str
    baseline_sha256: str
    soname: str
    library_path: str
    source_files: dict[str, str]


def baseline() -> Baseline:
    path = Path(__file__).resolve().parent / "sqlite-baseline.json"
    if not path.exists():
        path = Path(__file__).resolve().parents[1] / "runtime/sqlite-baseline.json"
    return cast(Baseline, json.loads(path.read_text()))


def build_defines() -> tuple[str, ...]:
    return tuple(
        "-DSQLITE_" + option
        for option in baseline()["compile_options"]
        if option not in AUTO_OPTIONS and not option.startswith("COMPILER=")
    )


def _source_path(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise ValueError("invalid source path")


def _fossil_name(value: str) -> str:
    escapes = {
        "s": " ",
        "n": "\n",
        "t": "\t",
        "r": "\r",
        "v": "\v",
        "f": "\f",
        "\\": "\\",
    }

    def decode(match: re.Match[str]) -> str:
        if match[1] not in escapes:
            raise ValueError("invalid Fossil escape")
        return escapes[match[1]]

    return re.sub(r"\\(.)", decode, value)


def verify_archive(
    data: bytes,
    *,
    expected_sha3: str = SOURCE_SHA3,
    expected_identity: str = SOURCE_UUID,
) -> dict[str, bytes]:
    """Authenticate the complete archive before executing any included program."""
    if len(data) > MAX_ARCHIVE or hashlib.sha3_256(data).hexdigest() != expected_sha3:
        raise ValueError("source archive SHA3-256 mismatch")
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > 5000:
            raise ValueError("source member bound")
        for item in archive.infolist():
            name = item.filename
            _source_path(name.rstrip("/"))
            if not name.startswith("sqlite-src-3540100/") or name in seen:
                raise ValueError("source root or duplicate member")
            seen.add(name)
            mode = stat.S_IFMT(item.external_attr >> 16)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
                raise ValueError("source special/encrypted member")
            if item.is_dir():
                if item.file_size or mode not in (0, stat.S_IFDIR):
                    raise ValueError("source directory shape")
                continue
            if mode == stat.S_IFDIR:
                raise ValueError("source directory mismatch")
            total += item.file_size
            if item.file_size > 32 * 1024 * 1024 or total > MAX_SOURCE:
                raise ValueError("source expansion bound")
            files[name.partition("/")[2]] = archive.read(item)
    manifest = files.get("manifest", b"")
    lines = manifest.splitlines(keepends=True)
    if lines and lines[-1].startswith(b"# Remove this line"):
        manifest = b"".join(lines[:-1])
    if hashlib.sha3_256(manifest).hexdigest() != expected_identity:
        raise ValueError("Fossil manifest identity mismatch")
    if files.get("manifest.uuid") != (expected_identity + "\n").encode():
        raise ValueError("Fossil UUID mismatch")
    if files.get("VERSION") != (VERSION + "\n").encode():
        raise ValueError("source version mismatch")
    declared: set[str] = set()
    for line in manifest.decode("utf-8").splitlines():
        if not line.startswith("F "):
            continue
        fields = line.split(" ")
        if len(fields) not in (3, 4) or (len(fields) == 4 and fields[3] != "x"):
            raise ValueError("unsupported Fossil file card")
        name, digest = _fossil_name(fields[1]), fields[2]
        _source_path(name)
        if (
            name in declared
            or name not in files
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", digest)
        ):
            raise ValueError("manifest membership mismatch")
        actual = (
            hashlib.sha1(files[name], usedforsecurity=False).hexdigest()
            if len(digest) == 40
            else hashlib.sha3_256(files[name]).hexdigest()
        )
        if actual != digest:
            raise ValueError("manifest file digest mismatch")
        declared.add(name)
    if (
        not declared
        or set(files) - {"manifest", "manifest.uuid", "manifest.tags"} != declared
    ):
        raise ValueError("unlisted or missing source file")
    return files


def verify_options(options: list[str]) -> None:
    malformed = None
    if type(options) is not list:
        malformed = "options_type"
    elif len(options) > 256:
        malformed = "option_count"
    elif any(type(item) is not str for item in options):
        malformed = "option_type"
    elif any(not 1 <= len(item) <= 256 for item in options):
        malformed = "option_length"
    elif any(not all(" " <= char <= "~" for char in item) for item in options):
        malformed = "option_characters"
    if malformed is not None:
        raise ValueError(
            "SQLite compile option drift: "
            + json.dumps({"malformed": malformed}, sort_keys=True)
        )
    if (VERSION, SOURCE_ID) != (
        "3.54.1",
        "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76",
    ):
        raise ValueError("unreconciled SQLite source options")
    compilers = [item for item in options if item.startswith("COMPILER=")]
    expected = {
        item
        for item in baseline()["compile_options"]
        if not item.startswith("COMPILER=")
    } | {"MAX_SCHEMA=10000000"}
    if (
        len(compilers) != 1
        or not compilers[0][9:]
        or len(set(options)) != len(options)
        or set(options) - set(compilers) != expected
    ):
        counts = Counter(options)
        actual = set(options) - set(compilers)
        raise ValueError(
            "SQLite compile option drift: "
            + json.dumps(
                {
                    "missing": sorted(expected - actual),
                    "unexpected": sorted(actual - expected),
                    "duplicates": {
                        item: count
                        for item, count in sorted(counts.items())
                        if count > 1
                    },
                    "compiler_count": len(compilers),
                    "compilers": compilers,
                },
                sort_keys=True,
            )
        )


def _verify_private_file(path: Path, *, require_root: bool) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or (require_root and info.st_uid != 0)
    ):
        raise ValueError("private artifact ownership/type/mode")


def verify_artifact(
    path: Path, expected_sha256: str, *, require_root: bool = True
) -> None:
    _verify_private_file(path, require_root=require_root)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("private artifact SHA256 mismatch")


def mapped_sqlite_paths(maps: str) -> set[str]:
    paths: set[str] = set()
    for line in maps.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and PurePosixPath(fields[5]).name.startswith(
            "libsqlite3.so"
        ):
            paths.add(fields[5])
    return paths


def elf_machine(path: Path) -> str:
    with path.open("rb") as stream:
        header = stream.read(20)
    if len(header) != 20 or header[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError("expected ELF64 little-endian version1")
    machine = int.from_bytes(header[18:20], "little")
    if machine not in (62, 183):
        raise ValueError("unsupported ELF machine")
    return {62: "x86_64", 183: "aarch64"}[machine]


def _run(argv: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.run(
        argv, cwd=cwd, check=True, capture_output=True, text=True, timeout=1200
    ).stdout


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        raise ValueError("source redirect refused")


def download_source() -> bytes:
    opener = urllib.request.build_opener(_NoRedirect())
    with cast(HTTPResponse, opener.open(SOURCE_URL, timeout=90)) as response:
        if response.status != 200 or response.url != SOURCE_URL:
            raise ValueError("source HTTP identity")
        data: bytes = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError("source download bound")
    return data


def build(output: Path, work: Path) -> None:
    """Called in a fresh root-owned builder, never on an operator's live host."""
    if os.geteuid() != 0:
        raise ValueError("builder must be root")
    work.mkdir(mode=0o755)
    output.mkdir(mode=0o755)
    data = download_source()
    files = verify_archive(data)
    source = work / "source"
    source.mkdir()
    for name, value in files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            _ = stream.write(value)
        target.chmod(0o755 if value.startswith(b"#!") else 0o644)
    generated = work / "generated"
    generated.mkdir()
    configure = [str(source / "configure"), *CONFIGURE, "LDFLAGS=-Wl,-z,relro,-z,now"]
    _ = _run(configure, cwd=generated)
    # OPTS reaches both Lemon and C compilation; CPPFLAGS alone does not.
    make = [
        "make",
        "-j2",
        "OPTS=" + " ".join(build_defines()),
        "CFLAGS=-O2 -fPIC -fstack-protector-strong -D_FORTIFY_SOURCE=2",
        "LDFLAGS.math=-lm",
    ]
    _ = _run([*make, "sqlite3.c", "libsqlite3.so"], cwd=generated)
    for name, expected in files.items():
        path = source / name
        if not stat.S_ISREG(path.lstat().st_mode) or path.read_bytes() != expected:
            raise ValueError("canonical source changed during generation")
    library = generated / "libsqlite3.so"
    import _sqlite3

    extension = Path(_sqlite3.__file__)
    dynamic = _run(["readelf", "-dW", str(library)])
    program_headers = _run(["readelf", "-lW", str(library)])
    extension_dynamic = _run(["readelf", "-dW", str(extension)])
    if (
        "Library soname: [libsqlite3.so.0]" not in dynamic
        or "Shared library: [libsqlite3.so.0]" not in extension_dynamic
    ):
        raise ValueError("ELF SONAME/CPython dependency")
    if any(token in extension_dynamic + dynamic for token in ("(RPATH)", "(RUNPATH)")):
        raise ValueError("unexpected ELF loader path")
    if (
        re.search(r"^\s*GNU_RELRO\s", program_headers, re.MULTILINE) is None
        or re.search(
            r"\(BIND_NOW\)|\(FLAGS(?:_1)?\)[^\n]*\b(?:BIND_NOW|NOW)\b", dynamic
        )
        is None
    ):
        raise ValueError("ELF RELRO/BIND_NOW hardening missing")
    exports = _run(["nm", "-D", "--defined-only", str(library)])
    undefined = _run(["nm", "-D", "--undefined-only", str(extension)])
    exported = {
        line.split()[-1].partition("@")[0]
        for line in exports.splitlines()
        if line.split()
    }
    needed = {
        line.split()[-1].partition("@")[0]
        for line in undefined.splitlines()
        if line.split() and line.split()[-1].startswith("sqlite3_")
    }
    if not needed or not (needed | set(REQUIRED_EXPORTS)) <= exported:
        raise ValueError("missing SQLite ELF exports")
    destination = output / "lib"
    destination.mkdir()
    _ = shutil.copyfile(library, destination / "libsqlite3.so.0")
    (destination / "libsqlite3.so.0").chmod(0o644)
    _ = shutil.copyfile(Path(__file__), output / "sqlite_runtime.py")
    baseline_source = (
        Path(__file__).resolve().parents[1] / "runtime/sqlite-baseline.json"
    )
    _ = shutil.copyfile(baseline_source, output / "sqlite-baseline.json")
    provenance: dict[str, object] = {
        "schema": 1,
        "base": BASE,
        "version": VERSION,
        "source_id": SOURCE_ID,
        "source_url": SOURCE_URL,
        "source_sha3_256": SOURCE_SHA3,
        "download_sha256": hashlib.sha256(data).hexdigest(),
        "manifest_sha3_256": SOURCE_ID.split()[-1],
        "source_files": {
            name: hashlib.sha256(value).hexdigest()
            for name, value in sorted(files.items())
        },
        "generated_sha256": {
            name: _sha(generated / name)
            for name in (
                "parse.c",
                "parse.h",
                "sqlite3.c",
                "sqlite3.h",
                "Makefile",
                "config.log",
            )
        },
        "configure": configure[1:],
        "make": make,
        "compiler": _run(["cc", "--version"]),
        "packages": _run(["dpkg-query", "-W", "-f=${Package}=${Version}\n"]),
        "machine": platform.machine(),
        "python": sys.version,
        "platform": platform.platform(),
        "soname": "libsqlite3.so.0",
        "library_path": str(PREFIX / "lib/libsqlite3.so.0"),
        "library_sha256": _sha(destination / "libsqlite3.so.0"),
        "extension_sha256": _sha(extension),
        "build_script_sha256": _sha(Path(__file__)),
        "baseline_sha256": _sha(baseline_source),
        "library_dynamic": dynamic,
        "library_program_headers": program_headers,
        "extension_dynamic": extension_dynamic,
        "library_exports": sorted(exported),
        "extension_sqlite_imports": sorted(needed),
        "library_versions": _run(["readelf", "--version-info", str(library)]),
        "library_elf_header": _run(["readelf", "-hW", str(library)]),
        "extension_elf_header": _run(["readelf", "-hW", str(extension)]),
        "qualification": "BUILD_ONLY_NOT_WAL_APPLICATION_OR_NATIVE_RELEASE_QUALIFICATION",
    }
    # Separate interpreter: _sqlite3 imported above already mapped the Debian copy.
    observation = _run(
        [
            sys.executable,
            "-B",
            "-c",
            "import ctypes,json,sys; x=ctypes.CDLL(sys.argv[1]); x.sqlite3_sourceid.restype=ctypes.c_char_p; x.sqlite3_compileoption_get.restype=ctypes.c_char_p; a=[]; i=0\nwhile (v:=x.sqlite3_compileoption_get(i)) is not None: a.append(v.decode()); i+=1\nprint(json.dumps({'source_id':x.sqlite3_sourceid().decode(),'options':a}))",
            str(library),
        ]
    )
    observed = cast(dict[str, object], json.loads(observation))
    if observed["source_id"] != SOURCE_ID:
        raise ValueError("generated source ID mismatch")
    options = cast(list[str], observed["options"])
    verify_options(options)
    provenance["compile_options"] = options
    _ = (output / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n"
    )
    for artifact in output.rglob("*"):
        artifact.chmod(0o755 if artifact.is_dir() else 0o644)


def _verify_schema_limit() -> dict[str, int]:
    import sqlite3

    # Pinned src/sqlite.h.in:4467; the unchanged adapter forwards integer IDs.
    category = 13
    with closing(sqlite3.connect(":memory:")) as connection:
        default = connection.getlimit(category)
        if default != 10000000:
            raise ValueError("SQLite schema limit default drift")
        try:
            if (
                connection.setlimit(category, 5) != default
                or connection.getlimit(category) != 5
            ):
                raise ValueError("SQLite schema limit adjustment failed")
            for sql in (
                "CREATE TABLE t1(a,b,c UNIQUE)",
                "CREATE INDEX x1 ON t1(a)",
                "CREATE VIEW v1 AS SELECT a+b AS x FROM t1",
            ):
                _ = connection.execute(sql)
            try:
                _ = connection.execute(
                    "CREATE TRIGGER r1 AFTER INSERT ON t1 BEGIN SELECT true; END"
                )
            except sqlite3.Error as error:
                try:
                    code: object = getattr(error, "sqlite_errorcode", None)
                    message: object = str(error)
                    matches = (
                        code == sqlite3.SQLITE_ABORT
                        and message == "too many schema objects"
                    )
                except Exception:
                    raise ValueError(
                        "SQLite schema overflow error mismatch: "
                        + json.dumps({"malformed": "unavailable"}, sort_keys=True)
                    ) from None
                if not matches:
                    detail: dict[str, object]
                    try:
                        name: object = getattr(error, "sqlite_errorname", None)
                        malformed = None
                        if type(code) is not int:
                            malformed = "code_type"
                        elif not 1 <= code <= 2147483647:
                            malformed = "code_range"
                        elif type(name) is not str:
                            malformed = "name_type"
                        elif not 1 <= len(name) <= 64:
                            malformed = "name_length"
                        elif not all(" " <= char <= "~" for char in name):
                            malformed = "name_characters"
                        elif type(message) is not str:
                            malformed = "message_type"
                        elif not 1 <= len(message) <= 512:
                            malformed = "message_length"
                        elif not all(" " <= char <= "~" for char in message):
                            malformed = "message_characters"
                        detail = (
                            {"malformed": malformed}
                            if malformed is not None
                            else {
                                "sqlite_errorcode": code,
                                "sqlite_errorname": name,
                                "message": message,
                            }
                        )
                    except Exception:
                        detail = {"malformed": "unavailable"}
                    raise ValueError(
                        "SQLite schema overflow error mismatch: "
                        + json.dumps(detail, sort_keys=True)
                    ) from None
            else:
                raise ValueError("SQLite schema overflow was not refused")
        finally:
            _ = connection.setlimit(category, default)
            if connection.getlimit(category) != default:
                raise ValueError("SQLite schema limit restoration failed")
    with closing(sqlite3.connect(":memory:")) as fresh:
        fresh_default = fresh.getlimit(category)
        if fresh_default != 10000000:
            raise ValueError("SQLite fresh schema limit default drift")
    return {"default": default, "fixture_limit": 5, "fresh_connection": fresh_default}


def verify_runtime(prefix: Path = PREFIX) -> dict[str, object]:
    """Run in the actual final image, through unchanged CPython sqlite3."""
    import _sqlite3
    import sqlite3

    for directory in (prefix, prefix.parent, prefix.parent.parent, prefix / "lib"):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("private directory ownership/mode")
    provenance_path = prefix / "provenance.json"
    _verify_private_file(provenance_path, require_root=True)
    provenance = cast(Provenance, json.loads(provenance_path.read_text()))
    if (
        provenance["schema"],
        provenance["base"],
        provenance["version"],
        provenance["source_id"],
        provenance["source_sha3_256"],
        provenance["machine"],
    ) != (1, BASE, VERSION, SOURCE_ID, SOURCE_SHA3, platform.machine()):
        raise ValueError("provenance identity mismatch")
    library = prefix / "lib/libsqlite3.so.0"
    if provenance["soname"] != "libsqlite3.so.0" or provenance["library_path"] != str(
        library
    ):
        raise ValueError("private library path/SONAME drift")
    verify_artifact(library, provenance["library_sha256"])
    verify_artifact(prefix / "sqlite_runtime.py", provenance["build_script_sha256"])
    verify_artifact(prefix / "sqlite-baseline.json", provenance["baseline_sha256"])
    extension = Path(_sqlite3.__file__)
    verify_artifact(extension, provenance["extension_sha256"])
    if (
        elf_machine(library) != platform.machine()
        or elf_machine(extension) != platform.machine()
    ):
        raise ValueError("runtime ELF machine mismatch")
    if mapped_sqlite_paths(Path("/proc/self/maps").read_text()) != {str(library)}:
        raise ValueError("CPython did not exclusively map private SQLite")
    if sqlite3.sqlite_version != VERSION or sqlite3.threadsafety != 3:
        raise ValueError("SQLite version/serialized threading")
    features: dict[str, bool] = {}
    for feature, enabled in baseline()["features"].items():
        parent, _, name = feature.rpartition(".")
        owner = sqlite3.Connection if parent == "Connection" else sqlite3
        features[feature] = hasattr(owner, name)
        if features[feature] != enabled:
            raise ValueError("CPython SQLite API capability drift")
    handle = ctypes.CDLL(str(library))
    for symbol in REQUIRED_EXPORTS:
        getattr(handle, symbol)
    defaults: dict[str, int] = {}
    with closing(sqlite3.connect(":memory:")) as connection:
        source_id = cast(
            str, connection.execute("SELECT sqlite_source_id()").fetchone()[0]
        )
        if source_id != SOURCE_ID:
            raise ValueError("SQL source identity mismatch")
        if (
            connection.execute('SELECT "legacy DQS literal"').fetchone()[0]
            != "legacy DQS literal"
        ):
            raise ValueError("DQS compatibility drift")
        option_rows = cast(
            list[tuple[str]], connection.execute("PRAGMA compile_options").fetchall()
        )
        options = [row[0] for row in option_rows]
        verify_options(options)
        if options != provenance["compile_options"]:
            raise ValueError("build/runtime option mismatch")
        for pragma, expected in MEMORY_DEFAULTS.items():
            defaults[pragma] = cast(
                int, connection.execute("PRAGMA " + pragma).fetchone()[0]
            )
            if defaults[pragma] != expected:
                raise ValueError("SQLite default drift: " + pragma)
        _ = connection.execute("CREATE TABLE capability(x)")
        _ = connection.executemany(
            "INSERT INTO capability VALUES (?)", [(1,), (2,), (3,)]
        )
        _ = connection.execute("UPDATE capability SET x=x+1 ORDER BY x LIMIT 1")
        _ = connection.execute("DELETE FROM capability ORDER BY x DESC LIMIT 1")
        if connection.execute("SELECT count(*) FROM capability").fetchone()[0] != 2:
            raise ValueError("UPDATE/DELETE LIMIT parser capability")
        for module in ("fts3", "fts4", "fts5"):
            _ = connection.execute(
                f"CREATE VIRTUAL TABLE {module}_check USING {module}(text)"
            )
            _ = connection.execute(f"INSERT INTO {module}_check VALUES ('mangarr')")
            if connection.execute(
                f"SELECT text FROM {module}_check WHERE {module}_check MATCH 'mangarr'"
            ).fetchall() != [("mangarr",)]:
                raise ValueError("FTS query capability")
        _ = connection.execute(
            "CREATE VIRTUAL TABLE rtree_check USING rtree(id, lo, hi)"
        )
        _ = connection.execute("INSERT INTO rtree_check VALUES (1, 2, 3)")
        if connection.execute(
            "SELECT id FROM rtree_check WHERE lo<=2 AND hi>=3"
        ).fetchall() != [(1,)]:
            raise ValueError("RTree query capability")
        if connection.execute(
            "SELECT json_extract('{\"x\":7}', '$.x'), sqrt(81)"
        ).fetchone() != (7, 9.0):
            raise ValueError("JSON/math capability")
        limits = {
            "function_arg": connection.getlimit(sqlite3.SQLITE_LIMIT_FUNCTION_ARG),
            "variable_number": connection.getlimit(
                sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
            ),
        }
        if limits != {"function_arg": 127, "variable_number": 250000}:
            raise ValueError("SQLite function/variable limit drift")
        connection.commit()
        with closing(sqlite3.connect(":memory:")) as target:
            connection.backup(target)
            if target.execute("SELECT x FROM capability ORDER BY x").fetchall() != [
                (2,),
                (2,),
            ]:
                raise ValueError("backup content mismatch")
            target.deserialize(connection.serialize())
            if target.execute("SELECT x FROM capability ORDER BY x").fetchall() != [
                (2,),
                (2,),
            ]:
                raise ValueError("serialization content mismatch")
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup/serialization integrity")
    return {
        "status": "PRIVATE_RUNTIME_IDENTITY_AND_CAPABILITIES_VERIFIED",
        "source_id": source_id,
        "library_path": str(library),
        "soname": provenance["soname"],
        "library_sha256": provenance["library_sha256"],
        "extension_path": str(extension),
        "extension_sha256": provenance["extension_sha256"],
        "elf_machine": elf_machine(library),
        "machine": platform.machine(),
        "compile_options": options,
        "features": features,
        "defaults": defaults,
        "limits": limits,
        "schema_limit": _verify_schema_limit(),
        "wal_source_sha256": provenance["source_files"]["src/wal.c"],
        "observation_scope": "THIS_PROBE_PROCESS_NOT_APPLICATION_SERVER",
        "wal_regression_qualified": False,
        "application_qualified": False,
    }


def _wal_actor(database: str, role: str, ready: Event) -> None:
    import sqlite3

    with closing(sqlite3.connect(database, timeout=5)) as connection:
        if not ready.wait(10):
            raise ValueError("WAL worker start bound")
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            raise ValueError("WAL worker mode drift")
        if role == "writer":
            for number in range(200):
                with connection:
                    _ = connection.execute(
                        "INSERT INTO exercise VALUES (?, ?)", (number, "committed")
                    )
        elif role == "checkpoint":
            for _ in range(200):
                row = cast(
                    tuple[int, int, int] | None,
                    connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone(),
                )
                if row is None or len(row) != 3 or row[0] not in (0, 1):
                    raise ValueError("checkpoint result")
        elif role == "crash":
            _ = connection.execute("INSERT INTO exercise VALUES (10000, 'durable')")
            connection.commit()
            _ = connection.execute("INSERT INTO exercise VALUES (10001, 'uncommitted')")
            # Deliberately bypass close/atexit, after one acknowledged commit.
            os._exit(0)
        else:
            raise ValueError("unknown WAL actor")


def _join_workers(
    workers: list[BaseProcess],
    *,
    start_attempted: set[BaseProcess] | None = None,
    abort: bool = False,
) -> None:
    identified: list[BaseProcess] = []
    incomplete = False
    failed = False
    for worker in workers:
        try:
            pid = worker.pid
        except BaseException:
            incomplete = True
            continue
        if type(pid) is int and pid > 0:
            identified.append(worker)
        elif (
            pid is None
            and start_attempted is not None
            and worker not in start_attempted
        ):
            # Enrollment preceded dispatch, and dispatch was never entered.
            try:
                worker.close()
            except BaseException:
                incomplete = True
        else:
            # A failed start can spawn before publishing identity. Never signal
            # or claim to reap that unknown child; the outer fence must resolve it.
            incomplete = True
    if not abort:
        for worker in identified:
            try:
                worker.join(60)
            except BaseException:
                incomplete = True
            try:
                if worker.exitcode != 0:
                    failed = True
            except BaseException:
                incomplete = True
    for worker in identified:
        try:
            alive = worker.is_alive()
        except BaseException:
            incomplete = True
            alive = True
        if alive:
            try:
                worker.terminate()
            except BaseException:
                incomplete = True
    for worker in identified:
        try:
            worker.join(5)
        except BaseException:
            incomplete = True
        try:
            alive = worker.is_alive()
        except BaseException:
            incomplete = True
            alive = True
        if alive:
            try:
                worker.kill()
            except BaseException:
                incomplete = True
        try:
            worker.join(5)
        except BaseException:
            incomplete = True
        try:
            terminal = not worker.is_alive() and worker.exitcode is not None
        except BaseException:
            incomplete = True
            terminal = False
        if terminal:
            try:
                worker.close()
            except BaseException:
                incomplete = True
        else:
            incomplete = True
    if incomplete:
        raise ValueError("WAL actor cleanup incomplete; outer process fence required")
    if failed:
        raise ValueError("WAL actor failed or exceeded bound")


def qualify_wal(workspace: Path) -> dict[str, object]:
    """Bounded disposable built-image regression, NOT upstream race/fix proof."""
    import sqlite3

    observation = verify_runtime()
    workspace.mkdir(mode=0o700)
    _ = (workspace / "OWNER.json").write_text(
        json.dumps(
            {"purpose": "disposable SQLite WAL qualification", "source_id": SOURCE_ID}
        )
        + "\n"
    )
    database = workspace / "qualification.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute("PRAGMA auto_vacuum").fetchone()[0] != 1:
            raise ValueError("fresh disk auto-vacuum default drift")
        if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise ValueError("WAL unavailable")
        if (
            connection.execute("PRAGMA synchronous").fetchone()[0] != 2
            or connection.execute("PRAGMA locking_mode").fetchone()[0] != "normal"
        ):
            raise ValueError("WAL synchronous/locking default drift")
        _ = connection.execute(
            "CREATE TABLE exercise(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.commit()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    for roles in (("writer", "checkpoint"), ("crash",)):
        workers: list[BaseProcess] = []
        start_attempted: set[BaseProcess] = set()
        try:
            for role in roles:
                worker = context.Process(
                    target=_wal_actor, args=(str(database), role, ready)
                )
                workers.append(worker)
                start_attempted.add(worker)
                worker.start()
            ready.set()
        except BaseException:
            _join_workers(workers, start_attempted=start_attempted, abort=True)
            raise
        _join_workers(workers, start_attempted=start_attempted)
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute("SELECT count(*) FROM exercise").fetchone()[0] != 201:
            raise ValueError("committed WAL rows lost or uncommitted row survived")
        if connection.execute(
            "SELECT id,value FROM exercise WHERE id>=10000"
        ).fetchall() != [(10000, "durable")]:
            raise ValueError("unclean-exit recovery mismatch")
        if connection.execute(
            "SELECT id,value FROM exercise WHERE id<10000 ORDER BY id"
        ).fetchall() != [(number, "committed") for number in range(200)]:
            raise ValueError("committed WAL content mismatch")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("recovered WAL integrity")
        if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() != (
            0,
            0,
            0,
        ):
            raise ValueError("final WAL checkpoint")
    return {
        **observation,
        "wal_exercise": "CONCURRENT_WRITER_CHECKPOINT_AND_UNCLEAN_EXIT_RECOVERY_PASSED",
        "joined_wal_workers": 3,
        "upstream_wal_reset_regression_qualified": False,
        "application_qualified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    _ = builder.add_argument("--output", type=Path, required=True)
    _ = builder.add_argument("--work", type=Path, required=True)
    _ = commands.add_parser("verify")
    qualifier = commands.add_parser("qualify-wal")
    _ = qualifier.add_argument("--workspace", type=Path, required=True)
    args = cast(dict[str, object], vars(parser.parse_args()))
    if args["command"] == "build":
        build(cast(Path, args["output"]), cast(Path, args["work"]))
    elif args["command"] == "verify":
        print(json.dumps(verify_runtime(), sort_keys=True))
    else:
        print(json.dumps(qualify_wal(cast(Path, args["workspace"])), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
