"""Rescan recovery acceptance tests: local injected faults, not live NFS proof.

The four errno-test functions are copied from test_nfs_file_claims_remaining.py
in the sibling file-claims worktree. Keep their acceptance assertions unchanged.
Crash tests kill a separate Python process after real filesystem/SQLite steps.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import signal
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import test_rescan_transactions as rescan_tests
from test_rescan_transactions import rescan_env as rescan_env

UNSUPPORTED = [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP]


def _unsupported_renameat2(
    monkeypatch: pytest.MonkeyPatch,
    error: int,
    *,
    source_contains: str | None = None,
) -> None:
    """Inject a real syscall errno, optionally only at a restoration boundary."""
    real_libc = ctypes.CDLL(None, use_errno=True)
    real_rename = real_libc.renameat2
    real_rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    real_rename.restype = ctypes.c_int

    class Rename:
        def __call__(
            self,
            source_fd: int,
            source: bytes,
            destination_fd: int,
            destination: bytes,
            flags: int,
        ) -> int:
            if source_contains is None or source_contains in os.fsdecode(source):
                ctypes.set_errno(error)
                return -1
            return real_rename(source_fd, source, destination_fd, destination, flags)

    libc = SimpleNamespace(renameat2=Rename())
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: libc)


@pytest.mark.parametrize("error", UNSUPPORTED)
def test_rescan_workflow_enriches_cbz_without_noreplace(
    rescan_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    import rescan

    rescan_tests._insert_volume(str(rescan_env["db_path"]), 1.0, "wanted")
    source = cast(Path, rescan_env["series_dir"]) / "Race Manga v01.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.jpg", b"original-page")
    _unsupported_renameat2(monkeypatch, error)

    result = rescan.rescan_series_folder(7)

    assert result["recovered"] == 1
    with zipfile.ZipFile(source) as archive:
        assert archive.read("001.jpg") == b"original-page"
        assert "ComicInfo.xml" in archive.namelist(), "probe skips NFS enrichment"
        assert b"<Series>Race Manga</Series>" in archive.read("ComicInfo.xml")


@pytest.mark.parametrize("error", UNSUPPORTED)
def test_rescan_private_publication_handles_unsupported_renameat2(
    tmp_path: Path,
    rescan_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    error: int,
) -> None:
    import rescan_file_recovery as recovery
    from dataclasses import asdict
    from file_mutation_lock import file_mutation_guard
    import private_file_claim as claims
    from test_rescan_recovery_protocol import _fixture

    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    stage = private_dir / "artifact.cbz"
    stage.write_bytes(b"staged-payload")
    destination = cast(Path, rescan_env["series_dir"]) / "published.cbz"
    _unsupported_renameat2(monkeypatch, error)

    source, _, target, context = _fixture(rescan_env)
    with file_mutation_guard(str(rescan_env["db_path"])) as guard:
        operation = recovery._reserve(
            target, context, recovery.fingerprint_path(str(source)), str(destination)
        )
        assert operation is not None
        recovery._allocate(operation, guard, "stage")
        with recovery._open(operation, guard, "stage") as carrier:
            Path(carrier.artifact_path).write_bytes(stage.read_bytes())
            with open(carrier.artifact_path, "rb") as handle:
                os.fsync(handle.fileno())
            os.fsync(carrier.fd)
            operation.fingerprints["stage"] = asdict(
                claims.fingerprint_regular(carrier)
            )
            recovery._store(operation)
        published = recovery._publish(operation, guard)

    assert published is not None, "private regular-file publication lacks link fallback"
    assert destination.read_bytes() == b"staged-payload"


@pytest.mark.parametrize("error", UNSUPPORTED)
def test_rescan_rollback_restores_source_when_only_restore_is_unsupported(
    rescan_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    import rescan

    volume_id = rescan_tests._insert_volume(str(rescan_env["db_path"]), 1.0, "wanted")
    source = cast(Path, rescan_env["series_dir"]) / "Race Manga v01.cbr"
    source.write_bytes(b"original-rar")

    def convert(staged_path: str) -> str:
        converted = str(Path(staged_path).with_suffix(".cbz"))
        with zipfile.ZipFile(converted, "w") as archive:
            archive.writestr("001.jpg", b"converted-page")
        return converted

    monkeypatch.setattr(rescan, "detect_file_type_magic", lambda path: "cbr")
    monkeypatch.setattr(rescan, "convert_cbr_to_cbz", convert)
    import rescan_file_recovery as recovery

    monkeypatch.setattr(recovery, "_commit", lambda *args: False)
    _unsupported_renameat2(monkeypatch, error, source_contains=".mangarr-claim-")

    result = rescan.rescan_series_folder(7)

    assert result["recovered"] == 1
    assert not source.with_suffix(".cbz").exists()
    with sqlite3.connect(str(rescan_env["db_path"])) as db:
        assert db.execute(
            "SELECT import_path FROM volumes WHERE id=?", (volume_id,)
        ).fetchone() == (str(source),)
    if not source.exists():
        retained = list(source.parent.glob(".mangarr-claim-*/*"))
        assert len(retained) == 1
        assert retained[0].read_bytes() == b"original-rar"
    assert source.is_file(), "rollback leaves the DB path absent on unsupported restore"
    assert source.read_bytes() == b"original-rar"


@pytest.mark.parametrize("error", UNSUPPORTED)
def test_failed_rescan_restore_retains_claim_and_occupied_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    import rescan

    source = tmp_path / "source.cbz"
    source.write_bytes(b"original")
    claim = rescan._claim_exact_path(str(source), rescan._fingerprint(source.stat()))
    assert claim is not None
    source.write_bytes(b"unrelated-winner")
    _unsupported_renameat2(monkeypatch, error)

    assert not rescan._restore_claim(claim)
    assert source.read_bytes() == b"unrelated-winner"
    assert Path(claim.claimed_path).read_bytes() == b"original"


_CHILD = r"""
import os
import signal
import sqlite3
import sys
import zipfile
from pathlib import Path

import rescan
import rescan_file_recovery as recovery
import shared

shared.DB_PATH = sys.argv[1]
shared.CONFIG.clear()
shared.CONFIG['folder_format'] = ''
checkpoint, extension = sys.argv[2:4]

def convert(path):
    converted = str(Path(path).with_suffix('.cbz'))
    with zipfile.ZipFile(converted, 'w') as archive:
        archive.writestr('001.jpg', b'converted-page')
    return converted

if extension == 'cbr':
    rescan.detect_file_type_magic = lambda path: 'cbr'
    rescan.convert_cbr_to_cbz = convert

def kill():
    print('checkpoint:' + checkpoint, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)

if checkpoint == 'after_claim':
    real = recovery._capture_source
    def wrapped(*args):
        result = real(*args)
        assert result is not None
        kill()
    recovery._capture_source = wrapped
elif checkpoint == 'after_publish':
    real = recovery._publish
    def wrapped(*args):
        result = real(*args)
        assert result is not None
        kill()
    recovery._publish = wrapped
elif checkpoint == 'after_db_commit':
    real = recovery._commit
    def wrapped(*args):
        result = real(*args)
        assert result is True
        kill()
    recovery._commit = wrapped
elif checkpoint == 'after_rollback_remove':
    real_publish = recovery._publish
    def lose_owner(*args):
        result = real_publish(*args)
        assert result is not None
        with sqlite3.connect(shared.DB_PATH) as db:
            db.execute("UPDATE volumes SET download_id='new-owner' WHERE series_id=7")
        return result
    recovery._publish = lose_owner
    real_cas = recovery._commit
    def lose_cas(*args):
        result = real_cas(*args)
        assert result is False
        return result
    recovery._commit = lose_cas
    real_remove = recovery._remove_publication
    def wrapped(*args):
        real_remove(*args)
        assert not Path(args[0].row['destination_path']).exists()
        kill()
    recovery._remove_publication = wrapped

print(rescan.rescan_series_folder(7), flush=True)
"""


def _run_child(
    env: dict[str, Any], checkpoint: str, extension: str
) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[2]
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(root / "app")
    child_env["MANGARR_CONFIG_DIR"] = str(Path(str(env["db_path"])).parent)
    return subprocess.run(
        [sys.executable, "-c", _CHILD, str(env["db_path"]), checkpoint, extension],
        cwd=root,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _seed_source(env: dict[str, Any], extension: str) -> tuple[int, Path, bytes]:
    volume_id = rescan_tests._insert_volume(str(env["db_path"]), 1.0, "wanted")
    source = cast(Path, env["series_dir"]) / f"Race Manga v01.{extension}"
    if extension == "cbr":
        source.write_bytes(b"original-rar")
    else:
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("001.jpg", b"original-page")
    return volume_id, source, source.read_bytes()


def _volume_state(env: dict[str, Any], volume_id: int) -> tuple[Any, ...]:
    with sqlite3.connect(str(env["db_path"])) as db:
        row = db.execute(
            "SELECT status,import_path,download_id FROM volumes WHERE id=?",
            (volume_id,),
        ).fetchone()
    assert row is not None
    return row


def _source_artifacts(env: dict[str, Any], source: Path) -> list[Path]:
    with sqlite3.connect(str(env["db_path"])) as db:
        rows = db.execute(
            "SELECT carriers_json FROM rescan_file_operations WHERE source_path=?",
            (str(source),),
        ).fetchall()
    return [
        Path(record["carrier_path"]) / "artifact"
        for row in rows
        if (record := json.loads(row[0])["source"]) is not None
        and (Path(record["carrier_path"]) / "artifact").is_file()
    ]


def _operation_carrier_dirs(source: Path) -> list[Path]:
    namespace = source.parent / ".mangarr-claims"
    return (
        list(p for p in namespace.iterdir() if p.is_dir()) if namespace.is_dir() else []
    )


@pytest.mark.parametrize("extension", ["cbr", "cbz"])
def test_native_child_workflow_control(
    rescan_env: dict[str, Any], extension: str
) -> None:
    volume_id, source, _ = _seed_source(rescan_env, extension)

    child = _run_child(rescan_env, "normal", extension)

    assert child.returncode == 0, child.stderr
    destination = source.with_suffix(".cbz")
    assert _volume_state(rescan_env, volume_id) == (
        "downloaded",
        str(destination),
        None,
    )
    with zipfile.ZipFile(destination) as archive:
        assert "ComicInfo.xml" in archive.namelist()
    assert not list(source.parent.glob(".mangarr-claim-*"))
    assert not _operation_carrier_dirs(source)


@pytest.mark.parametrize(
    ("extension", "checkpoint"),
    [
        ("cbr", "after_claim"),
        ("cbr", "after_publish"),
        ("cbr", "after_db_commit"),
        ("cbr", "after_rollback_remove"),
        ("cbz", "after_claim"),
        ("cbz", "after_publish"),
    ],
)
def test_fresh_rescan_recovers_killed_enrichment(
    rescan_env: dict[str, Any], extension: str, checkpoint: str
) -> None:
    volume_id, source, original = _seed_source(rescan_env, extension)

    killed = _run_child(rescan_env, checkpoint, extension)

    assert killed.returncode == -signal.SIGKILL, (killed.stdout, killed.stderr)
    assert f"checkpoint:{checkpoint}" in killed.stdout
    retained = _source_artifacts(rescan_env, source)
    assert len(retained) == 1
    assert retained[0].read_bytes() == original
    destination = source.with_suffix(".cbz")
    committed = checkpoint == "after_db_commit"
    expected_path = destination if committed else source
    expected_owner = "new-owner" if checkpoint == "after_rollback_remove" else None
    assert _volume_state(rescan_env, volume_id) == (
        "downloaded",
        str(expected_path),
        expected_owner,
    )
    published = checkpoint in ("after_publish", "after_db_commit")
    assert destination.exists() is published

    # A new interpreter has none of the dead process's _PathClaim objects.
    restarted = _run_child(rescan_env, "normal", extension)

    assert restarted.returncode == 0, restarted.stderr
    assert _volume_state(rescan_env, volume_id) == (
        "downloaded",
        str(expected_path),
        expected_owner,
    )
    if committed:
        assert destination.is_file()
        with zipfile.ZipFile(destination) as archive:
            assert "ComicInfo.xml" in archive.namelist()
    else:
        assert source.is_file(), "fresh rescan did not restore pre-commit original"
        assert source.read_bytes() == original
        if extension == "cbr":
            assert not destination.exists(), (
                "uncommitted conversion was not rolled back"
            )
    assert not _operation_carrier_dirs(source), (
        "fresh rescan has no authority to finish dead owner's claim"
    )
    assert not list(source.parent.glob(".mangarr-rescan-*")), (
        "dead owner's staging remains unregistered and unrecovered"
    )


@pytest.mark.parametrize("unsupported_restore", [False, True])
def test_real_conversion_cas_loser_preserves_original_and_new_db_owner(
    rescan_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    unsupported_restore: bool,
) -> None:
    import rescan
    import rescan_file_recovery as recovery

    volume_id, source, original = _seed_source(rescan_env, "cbr")
    real_publish = recovery._publish
    cas_results: list[bool] = []
    real_cas = recovery._commit

    def convert(path: str) -> str:
        converted = str(Path(path).with_suffix(".cbz"))
        with zipfile.ZipFile(converted, "w") as archive:
            archive.writestr("001.jpg", b"converted-page")
        return converted

    def lose_owner(*args: Any) -> Any:
        published = real_publish(*args)
        assert published is not None
        with sqlite3.connect(str(rescan_env["db_path"]), timeout=0.5) as db:
            db.execute(
                "UPDATE volumes SET download_id='new-owner' WHERE id=?", (volume_id,)
            )
        return published

    def observe_cas(*args: Any) -> bool:
        result = real_cas(*args)
        cas_results.append(result)
        return result

    monkeypatch.setattr(rescan, "detect_file_type_magic", lambda path: "cbr")
    monkeypatch.setattr(rescan, "convert_cbr_to_cbz", convert)
    monkeypatch.setattr(recovery, "_publish", lose_owner)
    monkeypatch.setattr(recovery, "_commit", observe_cas)
    if unsupported_restore:
        _unsupported_renameat2(
            monkeypatch, errno.EOPNOTSUPP, source_contains=".mangarr-claim-"
        )

    rescan.rescan_series_folder(7)

    assert cas_results == [False], "test must lose a real guarded SQLite UPDATE"
    assert _volume_state(rescan_env, volume_id) == (
        "downloaded",
        str(source),
        "new-owner",
    )
    assert not source.with_suffix(".cbz").exists()
    if not source.exists():
        retained = list(source.parent.glob(".mangarr-claim-*/*"))
        assert len(retained) == 1 and retained[0].read_bytes() == original
    assert source.is_file(), "CAS rollback did not restore the original DB path"
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "private_name",
    [".mangarr-claims", ".mangarr-claim-unregistered", ".mangarr-rescan-unregistered"],
)
def test_inventory_does_not_adopt_private_recovery_artifacts(
    rescan_env: dict[str, Any], private_name: str
) -> None:
    import rescan

    private = cast(Path, rescan_env["series_dir"]) / private_name
    private.mkdir()
    (private / "Race Manga v08.cbz").write_bytes(b"unregistered-private-bytes")

    result = rescan.rescan_series_folder(7)

    with sqlite3.connect(str(rescan_env["db_path"])) as db:
        volumes = db.execute(
            "SELECT import_path FROM volumes WHERE series_id=7"
        ).fetchall()
        count = db.execute("SELECT total_volumes FROM series WHERE id=7").fetchone()
    assert result["created"] == 0, "private artifacts were adopted as library volumes"
    assert volumes == []
    assert count == (None,), "private artifacts raised the local metadata count floor"
    assert (
        private / "Race Manga v08.cbz"
    ).read_bytes() == b"unregistered-private-bytes"


def test_live_filesystem_owner_blocks_rescan_shared_mutation(
    rescan_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import rescan
    from file_mutation_lock import file_mutation_guard

    _, source, original = _seed_source(rescan_env, "cbr")

    def convert(path: str) -> str:
        converted = str(Path(path).with_suffix(".cbz"))
        with zipfile.ZipFile(converted, "w") as archive:
            archive.writestr("001.jpg", b"converted-page")
        return converted

    monkeypatch.setattr(rescan, "detect_file_type_magic", lambda path: "cbr")
    monkeypatch.setattr(rescan, "convert_cbr_to_cbz", convert)

    with file_mutation_guard(str(rescan_env["db_path"])) as guard:
        rescan.rescan_series_folder(7)
        guard.verify()
        assert source.is_file(), "rescan mutated shared files behind another live owner"
        assert source.read_bytes() == original
        assert not source.with_suffix(".cbz").exists()
