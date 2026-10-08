"""Bounded hard-purge FILE namespace lifecycle with real SQLite/files."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import zipfile

import pytest
from starlette.requests import Request

from test_rescan_transactions import _insert_volume, rescan_env as rescan_env


@pytest.fixture
def purge_env(rescan_env, monkeypatch):
    from routers import series_

    real_prepare = series_._prepare_hard_delete_series

    def prepare(*args, **kwargs):
        result = real_prepare(*args, **kwargs)
        result["cover_path"] = str(Path(rescan_env["db_path"]).parent / "absent.jpg")
        return result

    monkeypatch.setattr(series_, "_prepare_hard_delete_series", prepare)
    return rescan_env


def _archive(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", b"actual synthetic page")


def _registered(env):
    import private_file_claim as claims
    from file_mutation_lock import file_mutation_guard

    parent = Path(env["series_dir"]) / "nested" / "v01"
    parent.mkdir(parents=True)
    with file_mutation_guard(env["db_path"]) as guard:
        with claims.ensure_namespace(guard, str(parent)) as namespace:
            root = Path(namespace.path)
    evidence = root / "unresolved-carrier" / "artifact"
    evidence.parent.mkdir(mode=0o700)
    evidence.write_bytes(b"never GC this evidence")
    ordinary = parent / "ordinary.cbz"
    _archive(ordinary)
    volume_id = _insert_volume(
        env["db_path"], 1, "downloaded", import_path=str(ordinary)
    )
    _insert_volume(env["db_path"], 2, "downloaded", import_path=str(env["series_dir"]))
    _insert_volume(env["db_path"], 3, "downloaded", import_path=str(parent))
    with sqlite3.connect(env["db_path"]) as db:
        db.execute("UPDATE series SET deleted_at=datetime('now','-31 days') WHERE id=7")
    return parent, root, evidence, ordinary, volume_id


def _registry(env):
    with sqlite3.connect(env["db_path"]) as db:
        return db.execute(
            "SELECT * FROM file_claim_namespaces ORDER BY parent_path"
        ).fetchall()


def _state(env):
    with sqlite3.connect(env["db_path"]) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "series",
                "volumes",
                "history",
                "file_claim_namespaces",
                "volume_file_deletions",
                "import_queue",
            )
        }


def _purge():
    from routers import series_

    return series_._run_hard_delete_series(7, remove_files=True, log_history=True)


@pytest.mark.parametrize("caller", ["service", "manual", "empty_bin", "reaper"])
def test_nested_overlapping_purge_keeps_namespace_parent_shells(purge_env, caller):
    from routers import series_
    from tasks import _run_recycle_bin_purge_once

    parent, root, evidence, ordinary, _ = _registered(purge_env)
    shells = [Path(purge_env["series_dir"]), parent.parent, parent, root]
    identities = [(p.stat().st_dev, p.stat().st_ino) for p in shells]
    marker = (root / "owner.json").read_bytes()
    registry = _registry(purge_env)
    for name in (".hidden", ".mangarr-claims-near", "ordinary-sibling"):
        _archive(Path(purge_env["series_dir"]) / name / "page.cbz")
    request = Request({"type": "http", "method": "POST", "headers": []})
    if caller == "service":
        assert _purge()["status"] == "purged"
    elif caller == "manual":
        assert asyncio.run(series_.purge_series(request, 7)).status_code == 303
    elif caller == "empty_bin":
        assert asyncio.run(series_.recycle_bin_empty(request)).status_code == 303
    else:
        assert _run_recycle_bin_purge_once(retention_days=30, remove_files=True) == 1
    assert [(p.stat().st_dev, p.stat().st_ino) for p in shells] == identities
    assert (root / "owner.json").read_bytes() == marker
    assert evidence.read_bytes() == b"never GC this evidence"
    assert _registry(purge_env) == registry
    assert not ordinary.exists()
    assert sorted(p.name for p in Path(purge_env["series_dir"]).iterdir()) == ["nested"]
    with sqlite3.connect(purge_env["db_path"]) as db:
        assert db.execute("SELECT id FROM series WHERE id=7").fetchall() == []
        assert db.execute("SELECT event_type FROM history").fetchall() == [
            ("series_purged",)
        ]


@pytest.mark.parametrize(
    "proof",
    [
        "null",
        "malformed",
        "marker_changed",
        "missing_root",
        "root_swapped",
        "parent_swapped",
    ],
)
def test_unproven_registry_retains_entire_affected_target(purge_env, proof):
    parent, root, evidence, ordinary, _ = _registered(purge_env)
    original = ordinary.read_bytes()
    if proof in {"null", "malformed"}:
        with sqlite3.connect(purge_env["db_path"]) as db:
            db.execute(
                "UPDATE file_claim_namespaces SET ownership_json=?",
                (None if proof == "null" else "{}",),
            )
    elif proof == "marker_changed":
        (root / "owner.json").write_bytes(b"unproven marker")
    elif proof in {"missing_root", "root_swapped"}:
        marker = (root / "owner.json").read_bytes()
        root.rename(parent / "retained-old-root")
        evidence = parent / "retained-old-root" / "unresolved-carrier" / "artifact"
        if proof == "root_swapped":
            root.mkdir(mode=0o700)
            (root / "owner.json").write_bytes(marker)
    else:
        retained = parent.with_name("retained-parent")
        parent.rename(retained)
        parent.mkdir()
        ordinary.write_bytes(original)
        root.mkdir(mode=0o700)
        (root / "owner.json").write_bytes(b"unknown replacement marker")
        evidence = retained / ".mangarr-claims" / "unresolved-carrier" / "artifact"
    registry = _registry(purge_env)
    assert _purge()["status"] == "purged"
    assert ordinary.read_bytes() == original
    assert evidence.read_bytes() == b"never GC this evidence"
    assert _registry(purge_env) == registry


@pytest.mark.parametrize("kind", ["artifact", "symlink"])
def test_direct_private_target_is_retained(purge_env, kind):
    parent, root, evidence, _, _ = _registered(purge_env)
    target = evidence
    if kind == "symlink":
        target = root / "unknown-link"
        target.symlink_to(evidence)
    with sqlite3.connect(purge_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE series_id=7", (str(target),))
    registry = _registry(purge_env)
    assert _purge()["status"] == "purged"
    assert target.exists()
    assert evidence.read_bytes() == b"never GC this evidence"
    assert parent.is_dir() and root.is_dir()
    assert _registry(purge_env) == registry


def test_unknown_reserved_boundary_and_shells_are_retained_without_registration(
    purge_env,
):
    tree = Path(purge_env["series_dir"])
    root = tree / "unregistered" / "deep" / ".mangarr-claims"
    root.mkdir(parents=True, mode=0o700)
    artifact = root / "artifact"
    artifact.write_bytes(b"unknown ownership")
    shell_inode = root.parent.stat().st_ino
    _archive(tree / "ordinary.cbz")
    _insert_volume(purge_env["db_path"], 1, "downloaded", import_path=str(tree))
    with sqlite3.connect(purge_env["db_path"]) as db:
        db.execute("UPDATE series SET deleted_at=CURRENT_TIMESTAMP WHERE id=7")
    assert _purge()["status"] == "purged"
    assert artifact.read_bytes() == b"unknown ownership"
    assert root.parent.stat().st_ino == shell_inode
    assert not (tree / "ordinary.cbz").exists()
    assert _registry(purge_env) == []


def test_live_guard_contention_leaves_database_history_and_files_unchanged(purge_env):
    from file_mutation_lock import file_mutation_guard

    _, root, evidence, ordinary, _ = _registered(purge_env)
    before = _state(purge_env)
    original = ordinary.read_bytes()
    with file_mutation_guard(purge_env["db_path"]):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert (
                pool.submit(_purge).result(timeout=5)["status"] == "import_in_progress"
            )
    assert _state(purge_env) == before
    assert ordinary.read_bytes() == original
    assert evidence.read_bytes() == b"never GC this evidence"
    assert root.is_dir()


def test_database_only_purge_does_not_require_filesystem_guard(purge_env):
    from file_mutation_lock import file_mutation_guard
    from routers import series_

    _, root, evidence, ordinary, _ = _registered(purge_env)
    original = ordinary.read_bytes()
    registry = _registry(purge_env)
    with file_mutation_guard(purge_env["db_path"]):
        assert (
            series_._run_hard_delete_series(7, remove_files=False)["status"] == "purged"
        )
    assert ordinary.read_bytes() == original
    assert root.is_dir() and evidence.is_file()
    assert _registry(purge_env) == registry


@pytest.mark.parametrize("active", ["deletion", "import"])
def test_active_operation_refuses_purge_without_mutation(purge_env, active):
    import volume_file_deletion

    _, _, _, ordinary, volume_id = _registered(purge_env)
    if active == "deletion":
        assert (
            volume_file_deletion.reserve_volume_file_deletion(7, volume_id).status
            == "reserved"
        )
    else:
        with sqlite3.connect(purge_env["db_path"]) as db:
            db.execute(
                "INSERT INTO import_queue(series_id,torrent_name,status) VALUES(7,'live','importing')"
            )
    before = _state(purge_env)
    original = ordinary.read_bytes()
    assert _purge()["status"] == "import_in_progress"
    assert _state(purge_env) == before
    assert ordinary.read_bytes() == original


def test_namespace_walk_and_removal_have_no_sqlite_writer_and_hold_guard(
    purge_env, monkeypatch
):
    import private_file_claim as claims
    from file_mutation_lock import FileMutationBusy, file_mutation_guard
    from routers import series_

    _registered(purge_env)
    _archive(Path(purge_env["series_dir"]) / "public-directory" / "ordinary.cbz")
    seen = []

    def probe(name):
        with sqlite3.connect(purge_env["db_path"], timeout=0.05) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO events(event_type,message) VALUES('probe',?)", (name,)
            )
        with pytest.raises(FileMutationBusy):
            with file_mutation_guard(purge_env["db_path"]):
                pytest.fail("purge lost owner guard")
        seen.append(name)

    real_open = claims.open_namespace

    @contextmanager
    def opened(*args):
        probe("open")
        with real_open(*args) as namespace:
            yield namespace

    monkeypatch.setattr(claims, "open_namespace", opened)
    for name in ("walk", "remove", "rmdir"):
        real = getattr(series_.os, name)

        def checked(*args, _real=real, _name=name, **kwargs):
            probe(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(series_.os, name, checked)
    monkeypatch.setattr(
        claims,
        "ensure_namespace",
        lambda *args: pytest.fail("purge allocates namespace"),
    )
    monkeypatch.setattr(
        claims,
        "gc_discarded_carrier",
        lambda *args: pytest.fail("purge runs carrier GC"),
    )
    assert _purge()["status"] == "purged"
    assert set(seen) == {"open", "walk", "remove", "rmdir"}


def test_cleanup_failure_closes_namespace_handles_and_preserves_registry(
    purge_env, monkeypatch
):
    import private_file_claim as claims
    from file_mutation_lock import file_mutation_guard
    from routers import series_

    _, root, evidence, ordinary, _ = _registered(purge_env)
    registry = _registry(purge_env)
    real_open = claims.open_namespace
    real_remove = series_.os.remove
    fds = []

    @contextmanager
    def opened(*args):
        with real_open(*args) as namespace:
            fds.extend((namespace.fd, namespace.parent_fd))
            yield namespace

    def fail(path):
        if path == str(ordinary):
            raise OSError("injected ordinary cleanup failure")
        return real_remove(path)

    monkeypatch.setattr(claims, "open_namespace", opened)
    monkeypatch.setattr(series_.os, "remove", fail)
    assert _purge()["status"] == "purged"
    assert fds
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert root.is_dir() and evidence.is_file()
    assert _registry(purge_env) == registry
    with file_mutation_guard(purge_env["db_path"]) as guard:
        guard.verify()


@pytest.mark.parametrize("private", [False, True])
def test_intermediate_alias_preserves_private_carrier(purge_env, private):
    parent, root, evidence, ordinary, _ = _registered(purge_env)
    alias = Path(purge_env["series_dir"]) / "alias"
    if private:
        alias.symlink_to(root, target_is_directory=True)
        target = alias / "unresolved-carrier"
    else:
        public = parent / "public"
        leaf = public / "leaf"
        leaf.mkdir(parents=True)
        (leaf / "ordinary.cbz").write_bytes(ordinary.read_bytes())
        alias.symlink_to(public, target_is_directory=True)
        target = alias / "leaf"
    registry = _registry(purge_env)
    before = evidence.read_bytes()
    with sqlite3.connect(purge_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE series_id=7", (str(target),))
    result = _purge()
    assert result["status"] == "purged"
    assert _registry(purge_env) == registry
    assert evidence.exists(), "purge deleted private carrier through intermediate alias"
    assert evidence.read_bytes() == before
    if private:
        assert target.is_dir()
    else:
        assert not target.exists()


@pytest.mark.parametrize("kind", ["artifact", "private_symlink", "public_symlink"])
def test_alias_target_keeps_final_symlink_policy(purge_env, kind):
    parent, root, evidence, _, _ = _registered(purge_env)
    alias = Path(purge_env["series_dir"]) / "alias"
    if kind == "public_symlink":
        target = alias
        alias.symlink_to(root, target_is_directory=True)
    else:
        alias.symlink_to(root, target_is_directory=True)
        target = alias / "unresolved-carrier" / "artifact"
        if kind == "private_symlink":
            link = evidence.parent / "private-link"
            link.symlink_to(evidence)
            target = alias / "unresolved-carrier" / "private-link"
    registry = _registry(purge_env)
    identities = [(p.stat().st_dev, p.stat().st_ino) for p in (parent, root)]
    with sqlite3.connect(purge_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE series_id=7", (str(target),))
    assert _purge()["status"] == "purged"
    assert evidence.read_bytes() == b"never GC this evidence"
    assert [(p.stat().st_dev, p.stat().st_ino) for p in (parent, root)] == identities
    assert _registry(purge_env) == registry
    if kind == "public_symlink":
        assert not target.is_symlink()
    else:
        assert target.exists()


@pytest.mark.parametrize("proof", ["valid", "null"])
def test_aliased_namespace_parent_preserves_shells_and_proof_policy(purge_env, proof):
    parent, root, evidence, ordinary, _ = _registered(purge_env)
    alias = Path(purge_env["series_dir"]) / "alias"
    alias.symlink_to(parent.parent, target_is_directory=True)
    target = alias / parent.name
    identities = [(p.stat().st_dev, p.stat().st_ino) for p in (parent, root)]
    before = ordinary.read_bytes()
    with sqlite3.connect(purge_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE series_id=7", (str(target),))
        if proof == "null":
            db.execute("UPDATE file_claim_namespaces SET ownership_json=NULL")
    registry = _registry(purge_env)
    assert _purge()["status"] == "purged"
    assert evidence.read_bytes() == b"never GC this evidence"
    assert [(p.stat().st_dev, p.stat().st_ino) for p in (parent, root)] == identities
    assert _registry(purge_env) == registry
    if proof == "null":
        assert ordinary.read_bytes() == before
    else:
        assert not ordinary.exists()


def test_alias_resolution_is_outside_writer_and_under_owner_guard(
    purge_env, monkeypatch
):
    from file_mutation_lock import FileMutationBusy, file_mutation_guard
    from routers import series_

    _, root, evidence, _, _ = _registered(purge_env)
    alias = Path(purge_env["series_dir"]) / "alias"
    alias.symlink_to(root, target_is_directory=True)
    target = alias / "unresolved-carrier"
    with sqlite3.connect(purge_env["db_path"]) as db:
        db.execute("UPDATE volumes SET import_path=? WHERE series_id=7", (str(target),))
    seen = []
    real_resolve = series_.os.path.realpath

    def resolve(path, *args, **kwargs):
        with sqlite3.connect(purge_env["db_path"], timeout=0.05) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO events(event_type,message) VALUES('probe','resolve')"
            )
        with pytest.raises(FileMutationBusy):
            with file_mutation_guard(purge_env["db_path"]):
                pytest.fail("alias resolution lost purge owner")
        seen.append(str(path))
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(series_.os.path, "realpath", resolve)
    assert _purge()["status"] == "purged"
    assert str(alias) in seen
    assert evidence.read_bytes() == b"never GC this evidence"


def test_resolved_namespace_lookup_failure_retains_postcommit_files(
    purge_env, monkeypatch
):
    from routers import series_

    parent, root, evidence, ordinary, _ = _registered(purge_env)
    original = ordinary.read_bytes()
    registry = _registry(purge_env)
    real_snapshot = series_._snapshot_purge_namespaces

    def snapshot(db, paths):
        if not db.in_transaction:
            raise sqlite3.OperationalError(
                "injected supplemental registry read failure"
            )
        return real_snapshot(db, paths)

    monkeypatch.setattr(series_, "_snapshot_purge_namespaces", snapshot)
    assert _purge()["status"] == "purged"
    assert ordinary.read_bytes() == original
    assert parent.is_dir() and root.is_dir()
    assert evidence.read_bytes() == b"never GC this evidence"
    assert _registry(purge_env) == registry
