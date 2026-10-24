"""Real source/installed ownership commands, never source fallback when selected."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import cast

import pytest

from file_identity_recovery import OwnershipInventory, RecoveryPlan
from test_file_identity_cli import (
    INVENTORY_VERSION_CASES,
    OWNERSHIP_GLOBAL_PREFIXES,
    cli_plan_inputs,
    cli_schema,
)
from test_file_identity_observation import (
    ProducerMedia,
    media_snapshot,
    producer_media as producer_media,
    producer_queue,
)
from test_file_identity_schema import ledger_path as ledger_path
from test_operator_cli import _operator_command
from test_operator_cli_packaging import MODULES, ROOT, verify


def json_object(encoded: str) -> dict[str, object]:
    value = cast(object, json.loads(encoded))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def row_data(row: sqlite3.Row) -> dict[str, object]:
    return {key: cast(object, row[key]) for key in row.keys()}


_WHEEL_PROBE = """
import base64, hashlib, importlib, importlib.metadata, json, pathlib, sys, sysconfig, zipfile
wheel, checkout, *names = sys.argv[1:]
dist = importlib.metadata.distribution('mangarr')
assert not json.loads(dist.read_text('direct_url.json') or '{}').get('dir_info', {}).get('editable')
roots = {pathlib.Path(sysconfig.get_path(k)).resolve() for k in ('purelib', 'platlib')}
assert all(not pathlib.Path(p).resolve().is_relative_to(pathlib.Path(checkout)) for p in sys.path if p)
records = {str(pathlib.Path(dist.locate_file(p)).resolve()): p for p in dist.files or ()}
with zipfile.ZipFile(wheel) as archive:
    metadata = archive.read(next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))).decode()
    from email.parser import Parser
    message = Parser().parsestr(metadata)
    assert message['Name'] == dist.metadata['Name'] == 'mangarr'
    assert message['Version'] == dist.version
    for name in names:
        module = importlib.import_module(name)
        path = pathlib.Path(module.__file__).resolve()
        assert any(path.is_relative_to(root) for root in roots), 'module outside site-packages'
        assert str(path) in records, 'module absent from distribution RECORD'
        record = records[str(path)]
        assert record.hash is not None and record.hash.mode == 'sha256'
        data = path.read_bytes()
        assert data == archive.read(name + '.py'), 'module differs from selected wheel'
        assert base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=') == record.hash.value
assert not {'main', 'schema', 'import_queue', 'routers'} & sys.modules.keys()
print(json.dumps({'modules': sorted(names), 'wheel_sha256': hashlib.sha256(pathlib.Path(wheel).read_bytes()).hexdigest()}))
"""


def operator_command(workdir: Path) -> tuple[list[str], dict[str, str]]:
    command, env = _operator_command(workdir)
    if "MANGARR_INSTALLED_CLI" in env:
        selected = Path(env.get("MANGARR_OPERATOR_WHEEL", ""))
        assert selected.is_absolute() and selected.is_file(), (
            "selected wheel is required"
        )
        verified = verify(selected.parent)
        assert verified.returncode == 0, verified.stderr
        report = json_object(verified.stdout)
        assert report["wheel"] == str(selected)
        interpreter = Path(command[0]).read_text().splitlines()[0][2:]
        probe = subprocess.run(
            [
                interpreter,
                "-I",
                "-B",
                "-c",
                _WHEEL_PROBE,
                str(selected),
                str(ROOT),
                *sorted(MODULES),
            ],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        provenance = json_object(probe.stdout)
        assert provenance == {
            "modules": sorted(MODULES),
            "wheel_sha256": report["sha256"],
        }
    return command, env


def run_cli(workdir: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    command, env = operator_command(workdir)
    return subprocess.run(
        [*command, *arguments],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
        umask=0o022,
    )


@pytest.mark.parametrize(
    "selection", ["", "relative", "/missing/mangarr", "unregistered"]
)
def test_invalid_installed_selection_never_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    if selection == "unregistered":
        import sys

        launcher = tmp_path / "mangarr"
        _ = launcher.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
        launcher.chmod(0o700)
        selection = str(launcher)
    monkeypatch.setenv("MANGARR_INSTALLED_CLI", selection)
    with pytest.raises(AssertionError):
        _ = operator_command(tmp_path)


@pytest.mark.parametrize("complete", [True, False])
def test_ownership_inspect_schema7_and_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    complete: bool,
) -> None:
    database, media = cli_schema(tmp_path, monkeypatch)
    with closing(sqlite3.connect(database)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone() == (7,)
        if not complete:
            _ = db.execute("PRAGMA user_version=6")
    before = database.read_bytes(), media_snapshot(media)
    output = tmp_path / "inventory"
    result = run_cli(
        tmp_path,
        [
            "--config-dir",
            str(database.parent),
            "ownership",
            "inspect",
            "--parent",
            str(media),
            "--output",
            str(output),
        ],
    )
    assert result.returncode == (0 if complete else 3), result.stderr
    assert result.stderr == ""
    inventory = OwnershipInventory.from_json(output.read_text())
    assert bool(inventory.unresolved) is not complete
    summary = json_object(result.stdout)
    assert summary["outcome"] == (
        "inspect_complete" if complete else "inspect_incomplete"
    )
    assert summary["sha256"] == inventory.sha256
    assert before == (database.read_bytes(), media_snapshot(media))


@pytest.mark.parametrize("wire_fault", [False, True])
def test_ownership_plan_complete_and_wire_privacy(
    tmp_path: Path, wire_fault: bool
) -> None:
    arguments = cli_plan_inputs(tmp_path)
    if wire_fault:
        _ = (tmp_path / "inventory").write_text('{"private-wire-sentinel":true}')
    names = ("inventory", "decisions", "declaration", "receipt")
    before = {n: (tmp_path / n).read_bytes() for n in names}
    result = run_cli(tmp_path, arguments)
    if wire_fault:
        assert (result.returncode, result.stdout, result.stderr) == (
            1,
            "",
            "ownership_wire\n",
        )
        assert not (tmp_path / "result").exists()
    else:
        assert result.returncode == 0 and result.stderr == ""
        plan = RecoveryPlan.from_json((tmp_path / "result").read_text())
        assert json_object(result.stdout)["sha256"] == plan.sha256
        assert json_object(result.stdout)["outcome"] == "plan_recorded"
    assert {n: (tmp_path / n).read_bytes() for n in names} == before
    assert not (tmp_path / "unused-config").exists()


def test_ownership_argument_privacy(tmp_path: Path) -> None:
    result = run_cli(tmp_path, ["--private-argv-sentinel", "ownership", "inspect"])
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "",
        "ownership_arguments\n",
    )


@pytest.mark.parametrize("prefix", OWNERSHIP_GLOBAL_PREFIXES)
def test_ownership_explicit_version_malformed_global_prefix_is_private(
    tmp_path: Path, prefix: list[str]
) -> None:
    invalid = run_cli(
        tmp_path,
        [
            *prefix,
            "ownership",
            "inspect",
            "--inventory-version",
            "2",
            "--parent",
            str(tmp_path),
            "--output",
            str(tmp_path / "result"),
        ],
    )
    assert invalid.returncode == 2
    assert invalid.stdout == "" and invalid.stderr == "ownership_arguments\n"
    assert "private-sentinel" not in invalid.stdout + invalid.stderr
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("option", ["--inventory-version=2", "--inventory-v=2"])
def test_ownership_plan_infers_version_without_cli_override(
    tmp_path: Path, option: str
) -> None:
    invalid = run_cli(tmp_path, [*cli_plan_inputs(tmp_path, plan_version=2), option])
    assert (invalid.returncode, invalid.stdout, invalid.stderr) == (
        2,
        "",
        "ownership_arguments\n",
    )
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("options,version", INVENTORY_VERSION_CASES)
def test_ownership_inventory_version_is_explicit_and_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    version: int | None,
) -> None:
    database, media = cli_schema(tmp_path, monkeypatch)
    output = tmp_path / "private-sentinel"
    before = database.read_bytes(), media_snapshot(media)
    result = run_cli(
        tmp_path,
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
        ],
    )
    assert "private-sentinel" not in result.stdout + result.stderr
    if version is None:
        assert result.returncode == 2
        assert result.stdout == "" and result.stderr == "ownership_arguments\n"
        assert not output.exists()
        assert not (database.parent / ".manga_arr.db.file-mutation.lock").exists()
    else:
        assert result.returncode == 0 and result.stderr == ""
        inventory = OwnershipInventory.from_json(output.read_text())
        assert inventory.version == version and inventory.unresolved == ()
        assert json_object(result.stdout) == {
            "outcome": "inspect_complete",
            "sha256": inventory.sha256,
            "owners": 0,
            "roles": 0,
            "decisions": 0,
            "retained": 0,
            "blocked": 0,
        }
    assert before == (database.read_bytes(), media_snapshot(media))


@pytest.mark.parametrize("inventory_version", [1, 2])
def test_unsupported_revision_marker_is_incomplete_not_schema8_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inventory_version: int
) -> None:
    database, media = cli_schema(tmp_path, monkeypatch)
    with closing(sqlite3.connect(database)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone() == (7,)
        # Deliberately unsupported marker only, not schema8 or migration evidence.
        _ = db.execute("PRAGMA user_version=8")
    before = database.read_bytes(), media_snapshot(media)
    output = tmp_path / "private-sentinel"
    result = run_cli(
        tmp_path,
        [
            "--config-dir",
            str(database.parent),
            "ownership",
            "inspect",
            "--parent",
            str(media),
            "--output",
            str(output),
            "--inventory-version",
            str(inventory_version),
        ],
    )
    assert result.returncode == 3 and result.stderr == ""
    assert "private-sentinel" not in result.stdout
    inventory = OwnershipInventory.from_json(output.read_text())
    assert inventory.version == inventory_version
    assert inventory.unresolved == ("reference_uncertain",)
    assert json_object(result.stdout) == {
        "outcome": "inspect_incomplete",
        "sha256": inventory.sha256,
        "owners": 0,
        "roles": 0,
        "decisions": 0,
        "retained": 0,
        "blocked": 1,
    }
    assert before == (database.read_bytes(), media_snapshot(media))


@pytest.mark.parametrize("versions", [(1, 1), (1, 2), (2, 2)])
def test_nonempty_producer_plan_is_recorded_without_effects(
    tmp_path: Path,
    producer_media: ProducerMedia,
    monkeypatch: pytest.MonkeyPatch,
    versions: tuple[int, int],
) -> None:
    from file_identity import OwnerRef
    from file_mutation_lock import file_mutation_guard
    from private_file_claim import ensure_namespace
    from test_file_identity_observation import producer_rescan
    from test_file_identity_plan import (
        capture_inventory,
        chosen,
        linked_documents,
        review_documents,
    )

    env = producer_media
    identifier = producer_rescan(env, "prepared")
    with file_mutation_guard(str(env.db_path)) as guard:
        with ensure_namespace(guard, str(env.library)):
            pass
    # Existing fixture capture has synthetic native observations, not NAS qualification.
    inventory = capture_inventory(env, monkeypatch)
    inventory_version, plan_version = versions
    if inventory_version == 2:
        inventory = OwnershipInventory.create(
            inventory.parent,
            inventory.closure,
            inventory.observed,
            inventory.unresolved,
            inventory.budget,
            inventory_version=2,
        )
    selection = chosen(
        inventory, OwnerRef("rescan", (identifier,)), "cancel-unmutated", {"source"}
    )
    arguments = cli_plan_inputs(tmp_path)
    names = ("inventory", "decisions", "declaration", "receipt")
    reviews = review_documents(inventory, selection)
    if plan_version == 2:
        reviews = linked_documents(reviews, [])
    for name, value in zip(
        names,
        (inventory.to_json(), *reviews),
        strict=True,
    ):
        _ = (tmp_path / name).write_text(value)
    before = (
        env.db_path.read_bytes(),
        media_snapshot(env.scope),
        [(tmp_path / n).read_bytes() for n in names],
    )
    result = run_cli(tmp_path, arguments)
    assert result.returncode == 0 and result.stderr == "", result.stderr
    plan = RecoveryPlan.from_json((tmp_path / "result").read_text())
    assert plan.version == plan_version and plan.inventory.version == inventory_version
    summary = json_object(result.stdout)
    assert summary["outcome"] == "plan_recorded"
    assert summary["sha256"] == plan.sha256
    assert summary["owners"] == len(inventory.closure.snapshots) > 0
    assert summary["decisions"] == len(plan.decisions) > 0
    assert summary["blocked"] == 0
    assert before == (
        env.db_path.read_bytes(),
        media_snapshot(env.scope),
        [(tmp_path / n).read_bytes() for n in names],
    )
    assert not (tmp_path / "unused-config").exists()


@pytest.mark.parametrize("version", [None, 1, 2])
def test_real_publication_nonnull_ready_admission_is_consumed(
    tmp_path: Path,
    producer_media: ProducerMedia,
    version: int | None,
) -> None:
    import import_publication as publication
    from import_plan import _FilePlan, _ImportPlan, publication_admission

    env = producer_media
    queue_id = producer_queue(env)
    owner = "a" * 32
    with closing(sqlite3.connect(env.db_path)) as db, db:
        db.row_factory = sqlite3.Row
        _ = db.execute(
            "UPDATE import_queue SET status='importing',lease_owner=?,lease_expires_at=datetime('now','+1 hour') WHERE id=?",
            (owner, queue_id),
        )
        queue = row_data(
            cast(
                sqlite3.Row,
                db.execute(
                    "SELECT * FROM import_queue WHERE id=?", (queue_id,)
                ).fetchone(),
            )
        )
        row = row_data(
            cast(
                sqlite3.Row,
                db.execute(
                    "SELECT * FROM import_queue_files WHERE queue_id=?", (queue_id,)
                ).fetchone(),
            )
        )
        series = row_data(
            cast(sqlite3.Row, db.execute("SELECT * FROM series WHERE id=7").fetchone())
        )
        file_id, src_path, filename = row["id"], row["src_path"], row["filename"]
        assert (
            type(file_id) is int
            and isinstance(src_path, str)
            and isinstance(filename, str)
        )
        ready = _FilePlan(
            file_id=file_id,
            src_path=src_path,
            filename=filename,
            dst_path=str(env.library / "Matrix v02.cbz"),
            import_kind="volume",
            file_type="cbz",
            proposed_vol=2,
            proposed_chap=None,
            chap_range_end=None,
            vol_range_start=None,
            vol_range_end=None,
            pack_type=None,
            is_special=0,
            special_title=None,
            has_volume_range=False,
            is_legacy_chapter_stub=False,
            is_legacy_chapter_recheck=False,
            plan_status="ready",
            plan_failure_reason="",
        )
        plan = _ImportPlan(
            queue, series, [], str(env.library), "copy", None, [ready], 7
        )
        admission = cast(dict[str, object], publication_admission(db, plan))
        assert set(cast(dict[str, object], admission["files"])) == {str(ready.file_id)}
        queue["_file_publication"] = {
            "version": 1,
            "decision": "pending",
            "admission": admission,
            "artifacts": {},
        }
    # This calls the real create_publication producer and allocates its real carrier.
    _ = publication.initialize_publication_filesystem(plan, owner)
    config = tmp_path / "external-config"
    config.mkdir(mode=0o700)
    database = config / "manga_arr.db"
    with (
        closing(sqlite3.connect(env.db_path)) as source,
        closing(sqlite3.connect(database)) as target,
    ):
        source.backup(target)
        frozen = json_object(
            cast(
                str,
                target.execute(
                    "SELECT queue_snapshot_json FROM import_publications WHERE queue_id=?",
                    (queue_id,),
                ).fetchone()[0],
            )
        )
        assert (
            cast(dict[str, object], frozen["_file_publication"])["admission"]
            == admission
        )
        assert target.execute(
            "SELECT file_id FROM import_publication_files WHERE plan_status='ready'"
        ).fetchall() == [(ready.file_id,)]
    before = database.read_bytes(), env.db_path.read_bytes(), media_snapshot(env.scope)
    output = tmp_path / "inventory"
    result = run_cli(
        tmp_path,
        [
            "--config-dir",
            str(config),
            "ownership",
            "inspect",
            "--parent",
            str(env.scope),
            "--output",
            str(output),
            *(["--inventory-version", str(version)] if version is not None else []),
        ],
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    inventory = OwnershipInventory.from_json(output.read_text())
    assert inventory.version == (version or 1)
    assert json_object(result.stdout) == {
        "outcome": "inspect_complete",
        "sha256": inventory.sha256,
        "owners": len(inventory.closure.snapshots),
        "roles": len(inventory.closure.roles),
        "decisions": 0,
        "retained": 0,
        "blocked": 0,
    }
    assert not inventory.unresolved
    roles = [r for r in inventory.closure.roles if r.role.file_id == ready.file_id]
    assert any(
        r.role.purpose == "admission_source" and r.path == ready.src_path for r in roles
    )
    assert any(
        r.role.purpose == "admission_destination" and r.path == ready.dst_path
        for r in roles
    )
    assert before == (
        database.read_bytes(),
        env.db_path.read_bytes(),
        media_snapshot(env.scope),
    )


def test_legacy_admin_backup_controls_use_same_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backups import SECRET_KEY_ENTRY
    from cryptography.fernet import Fernet

    database, _media = cli_schema(tmp_path, monkeypatch)
    key = Fernet.generate_key()
    _ = (database.parent / SECRET_KEY_ENTRY).write_bytes(key)
    with closing(sqlite3.connect(database)) as db, db:
        _ = db.execute(
            "INSERT INTO auth_admin VALUES(1,'fixture','not-a-password','now','now')"
        )
        _ = db.execute(
            "INSERT INTO auth_sessions VALUES('session',1,'now','now','later')"
        )
    prefix = ["--config-dir", str(database.parent)]
    reset = run_cli(tmp_path, [*prefix, "admin", "reset", "--yes"])
    assert reset.returncode == 0, reset.stderr
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT count(*) FROM auth_admin").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM auth_sessions").fetchone() == (0,)
    created = run_cli(tmp_path, [*prefix, "backup", "create"])
    assert created.returncode == 0, created.stderr
    archives = list((database.parent / "backups").glob("*.zip"))
    assert len(archives) == 1
    archive = str(archives[0])
    digest = hashlib.sha256(archives[0].read_bytes()).hexdigest()
    validated = run_cli(tmp_path, [*prefix, "backup", "validate", archive, "--json"])
    assert validated.returncode == 0, validated.stderr
    details = json_object(validated.stdout)
    assert details["selfContained"] is True and details["schemaVersion"] == 7
    refused = run_cli(tmp_path, [*prefix, "backup", "restore", archive])
    assert refused.returncode == 2
    restored = run_cli(tmp_path, [*prefix, "backup", "restore", archive, "--yes"])
    assert restored.returncode == 0, restored.stderr
    assert (database.parent / SECRET_KEY_ENTRY).read_bytes().strip() == key
    assert hashlib.sha256(archives[0].read_bytes()).hexdigest() == digest
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (7,)
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
