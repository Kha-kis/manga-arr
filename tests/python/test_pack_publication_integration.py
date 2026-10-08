"""Frozen pack and FILE391 composition through real queue/publication cleanup."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sqlite3
import subprocess
import zipfile
from pathlib import Path
from typing import Literal

import pytest

from contextlib import AbstractContextManager

from file_mutation_lock import FileMutationGuard, file_mutation_guard
import private_file_claim as claims
from test_import_pack_cleanup_durability import (
    _PackEnv,
    _pack_paths,
    pack_env as pack_env,
)
from test_publication_nfs_file_claims_391 import _unsupported_renameat2


@pytest.mark.parametrize("placement", ["private", "native"])
@pytest.mark.parametrize("mode", ["move", "copy"])
@pytest.mark.parametrize("shared_mode", [0o770, 0o775])
def test_generated_pack_publication_has_no_nested_namespace_or_operation_residue(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    placement: Literal["private", "native"],
    mode: Literal["move", "copy"],
    shared_mode: int,
) -> None:
    import import_download
    import import_execute
    import import_queue
    import main
    import shared

    assert Path(import_queue.__file__).resolve().parents[1] == Path.cwd()
    assert Path(import_execute.__file__).resolve().parents[1] == Path.cwd()
    for config in (main.CONFIG, shared.CONFIG):
        config["import_mode"] = mode
        config["remove_completed"] = "false"
        config["minimum_free_space_mb"] = "0"
    monkeypatch.setattr(import_execute, "_IMPORT_SEM", None)

    async def no_network(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(import_execute, "broadcast_queue_event", no_network)
    monkeypatch.setattr(import_download, "dispatch_download_notification", no_network)
    # Actual helper-controlled birth and FULL registry proof, never invented SQL.
    root = pack_env["pack_root"]
    root.mkdir(mode=0o750)
    with file_mutation_guard(pack_env["db_path"]) as guard:
        with claims.ensure_namespace(guard, str(root)):
            pass
    with sqlite3.connect(pack_env["db_path"]) as db:
        proof = json.loads(
            db.execute(
                "SELECT ownership_json FROM file_claim_namespaces WHERE parent_path=?",
                (str(root),),
            ).fetchone()[0]
        )
    assert proof["version"] == 2
    assert proof["creation_boundary"]["parent_mode"] == 0o750
    root.chmod(shared_mode)
    if placement == "private":
        _unsupported_renameat2(monkeypatch, errno.EINVAL)

    download = f"integrated-{placement}-{mode}"
    original = pack_env["tmp_path"] / download
    chapter = original / "Pack Series c001"
    chapter.mkdir(parents=True)
    (chapter / "001.jpg").write_bytes(b"original-page-bytes")
    with main.get_db() as db:
        queue_id, _ = import_queue._queue_import(
            db,
            1,
            download,
            "Pack Series c001",
            "magnet:" + download,
            None,
            str(original),
            respect_grab_claims=False,
        )
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        assert db.execute(
            "SELECT src_dir FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone() == (str(original),)
        source_path = Path(
            db.execute(
                "SELECT src_path FROM import_queue_files WHERE queue_id=?",
                (queue_id,),
            ).fetchone()[0]
        )
        ownership = json.loads(
            db.execute(
                "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
                " WHERE queue_id=?",
                (queue_id,),
            ).fetchone()[0]
        )
    actual = source_path.parent
    canonical, _ = _pack_paths(download)
    if placement == "private":
        assert (
            actual == Path(ownership["placement_carrier"]["carrier_path"]) / "artifact"
        )
        assert not canonical.exists()
    else:
        assert actual == canonical
    assert list(actual.rglob(".mangarr-claims")) == []

    observed: list[Path] = []
    ensure_namespace = claims.ensure_namespace

    def observe(
        guard: FileMutationGuard, parent_path: str
    ) -> AbstractContextManager[claims.NamespaceHandle]:
        observed.append(Path(parent_path))
        return ensure_namespace(guard, parent_path)

    monkeypatch.setattr(claims, "ensure_namespace", observe)
    assert asyncio.run(import_execute._guarded_execute_import(queue_id))
    with sqlite3.connect(pack_env["db_path"]) as db:
        publication = db.execute(
            "SELECT state,diagnostic,pack_cleanup_state FROM import_publications"
            " WHERE queue_id=? ORDER BY id DESC LIMIT 1",
            (queue_id,),
        ).fetchone()
        queue_status = db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        final = Path(
            db.execute(
                "SELECT final_path FROM import_publication_files WHERE publication_id="
                " (SELECT id FROM import_publications WHERE queue_id=? ORDER BY id DESC LIMIT 1)",
                (queue_id,),
            ).fetchone()[0]
        )
        registries = [
            Path(row[0])
            for row in db.execute(
                "SELECT parent_path FROM file_claim_namespaces ORDER BY parent_path"
            )
        ]
        history_count = db.execute(
            "SELECT COUNT(*) FROM history WHERE series_id=1 AND event_type='imported'"
        ).fetchone()[0]
    with zipfile.ZipFile(final) as archive:
        assert archive.read("001.jpg") == b"original-page-bytes"
    assert (chapter / "001.jpg").read_bytes() == b"original-page-bytes"
    assert history_count == 1
    nested = [
        parent for parent in observed if parent == actual or actual in parent.parents
    ]
    assert nested == [], (
        f"FILE cleanup bootstrapped a namespace inside disposable pack: {nested};"
        f" publication={publication}; queue={queue_status}; registries={registries}"
    )
    assert publication is not None and publication[0] == "deleted", publication
    assert publication[2] == "complete", publication
    # FILE391 retires the completed queue after its durable imported history.
    assert queue_status is None
    assert not actual.exists(), "terminal pack artifact must be discarded"
    assert {p.name for p in (root / ".mangarr-claims").iterdir()} == {"owner.json"}
    for parent in registries:
        namespace = parent / ".mangarr-claims"
        assert namespace.is_dir(), (
            f"registry points inside removed artifact: {namespace}"
        )
        assert {entry.name for entry in namespace.iterdir()} == {"owner.json"}
    assert os.stat(root).st_mode & 0o777 == shared_mode


def test_other_uid_replaced_queued_private_namespace_cannot_rebind_source(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_download
    import import_execute
    import import_queue
    import main
    import shared

    for config in (main.CONFIG, shared.CONFIG):
        config["import_mode"] = "copy"
        config["remove_completed"] = "false"
        config["minimum_free_space_mb"] = "0"
    monkeypatch.setattr(import_execute, "_IMPORT_SEM", None)

    async def no_network(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(import_execute, "broadcast_queue_event", no_network)
    monkeypatch.setattr(import_download, "dispatch_download_notification", no_network)
    root = pack_env["pack_root"]
    root.mkdir(mode=0o750)
    with file_mutation_guard(pack_env["db_path"]) as guard:
        with claims.ensure_namespace(guard, str(root)):
            pass
    root.chmod(0o770)
    _unsupported_renameat2(monkeypatch, errno.EINVAL)
    original = pack_env["tmp_path"] / "replaced-source"
    chapter = original / "Pack Series c001"
    chapter.mkdir(parents=True)
    (chapter / "001.jpg").write_bytes(b"original-page-bytes")
    with main.get_db() as db:
        queue_id, _ = import_queue._queue_import(
            db,
            1,
            "replaced-source",
            "Pack Series c001",
            "magnet:replaced-source",
            None,
            str(original),
            respect_grab_claims=False,
        )
    assert queue_id is not None
    with sqlite3.connect(pack_env["db_path"]) as db:
        source = Path(
            db.execute(
                "SELECT src_path FROM import_queue_files WHERE queue_id=?",
                (queue_id,),
            ).fetchone()[0]
        )
        ownership = json.loads(
            db.execute(
                "SELECT directory_ownership_json FROM import_pack_cleanup_reservations"
                " WHERE queue_id=?",
                (queue_id,),
            ).fetchone()[0]
        )
    assert (
        source.parent
        == Path(ownership["placement_carrier"]["carrier_path"]) / "artifact"
    )
    original_bytes = source.read_bytes()
    relative = source.relative_to(root)
    actor = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=16",
            "--memory=128m",
            "--memory-swap=128m",
            "--cpus=1",
            "--user",
            f"{os.geteuid() + 1}:{os.getegid()}",
            "--mount",
            f"type=bind,src={root},dst=/attack",
            "--entrypoint=python",
            "sha256:0b133d75d4e1cef74740f7ba87d227ec269e0e2cdde4f6392539a136eb7b9412",
            "-c",
            "import os,sys,zipfile; from pathlib import Path; "
            "root=Path('/attack'); print(os.geteuid(),flush=True); "
            "os.rename(root/'.mangarr-claims',root/'preserved-original-namespace'); "
            "target=root/sys.argv[1]; target.parent.mkdir(parents=True,mode=0o755); "
            "archive=zipfile.ZipFile(target,'w'); "
            "archive.writestr('001.jpg',b'foreign-page-bytes'); archive.close()",
            str(relative),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert actor.returncode == 0, actor.stderr
    assert actor.stdout.strip() == str(os.geteuid() + 1)
    assert source.stat().st_uid == os.geteuid() + 1
    preserved = root / "preserved-original-namespace" / Path(*relative.parts[1:])
    assert preserved.read_bytes() == original_bytes
    replacement = source.read_bytes()
    result = asyncio.run(import_execute._guarded_execute_import(queue_id))
    assert preserved.read_bytes() == original_bytes
    assert source.read_bytes() == replacement, (
        "consumer changed the foreign replacement"
    )
    with sqlite3.connect(pack_env["db_path"]) as db:
        publications = db.execute(
            "SELECT state,diagnostic FROM import_publications WHERE queue_id=?",
            (queue_id,),
        ).fetchall()
        finals = [
            Path(row[0])
            for row in db.execute(
                "SELECT final_path FROM import_publication_files WHERE publication_id IN"
                " (SELECT id FROM import_publications WHERE queue_id=?)",
                (queue_id,),
            )
        ]
    imported_payloads = []
    for final in finals:
        if final.is_file():
            with zipfile.ZipFile(final) as archive:
                imported_payloads.append(archive.read("001.jpg"))
    assert b"foreign-page-bytes" not in imported_payloads, (
        f"fresh FILE source fingerprints admitted unrelated replacement;"
        f" result={result}; publication={publications}; finals={finals}"
    )
