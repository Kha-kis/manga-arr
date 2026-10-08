"""Pack-owned directory evidence, independent of shared regular-file APIs."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


def _module():
    assert importlib.util.find_spec("private_pack_claim") is not None
    import private_pack_claim

    return private_pack_claim


def test_directory_proof_rejects_recreated_empty_path(tmp_path: Path) -> None:
    pack = _module()
    root = tmp_path / "tree"
    root.mkdir(mode=0o700)
    with pack.open_directory(str(root)) as fd:
        proof = pack.directory_proof(str(root), fd)
        root.rename(tmp_path / "original")
        root.mkdir(mode=0o700)
        with pytest.raises(pack.PackProofError):
            pack.verify_directory(fd, proof)
    assert root.is_dir()


@pytest.mark.parametrize("change", ["added", "changed", "missing", "symlink"])
def test_inventory_rejects_unrecorded_mutations(tmp_path: Path, change: str) -> None:
    pack = _module()
    root = tmp_path / "tree"
    root.mkdir()
    (root / "page.cbz").write_bytes(b"original")
    with pack.open_directory(str(root)) as fd:
        inventory = pack.inventory_tree(fd)
        if change == "added":
            (root / "extra").write_bytes(b"extra")
        elif change == "changed":
            (root / "page.cbz").write_bytes(b"changed")
        elif change == "missing":
            (root / "page.cbz").unlink()
        else:
            (root / "alias").symlink_to(root / "page.cbz")
        with pytest.raises((pack.PackProofError, OSError)):
            pack.verify_inventory(fd, inventory)
    assert root.exists()


def test_only_explicit_missing_inventory_paths_are_allowed(tmp_path: Path) -> None:
    pack = _module()
    (tmp_path / "a").write_bytes(b"a")
    (tmp_path / "b").write_bytes(b"b")
    with pack.open_directory(str(tmp_path)) as fd:
        inventory = pack.inventory_tree(fd)
        (tmp_path / "a").unlink()
        pack.verify_inventory(fd, inventory, allowed_missing=frozenset({"a"}))
        (tmp_path / "b").unlink()
        with pytest.raises(pack.PackProofError):
            pack.verify_inventory(fd, inventory, allowed_missing=frozenset({"a"}))


def test_original_owner_metadata_roundtrips_without_executor(tmp_path: Path) -> None:
    pack = _module()
    ownership = pack.PackOwnership("identity", "original-owner")
    assert pack.PackOwnership.from_json(ownership.to_json()) == ownership
    value = json.loads(ownership.to_json())
    assert "executor" not in value
    for invalid in [
        ownership.to_json().replace('"version":1', '"version":2'),
        ownership.to_json().replace('"version":1', '"version":true'),
        ownership.to_json()[:-1] + ',"version":1}',
    ]:
        with pytest.raises(pack.PackProofError):
            pack.PackOwnership.from_json(invalid)


def test_inventory_flushes_nested_directories_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack = _module()
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "aux").write_bytes(b"auxiliary")
    flushed: list[str] = []
    original = os.fsync

    def flush(fd: int) -> None:
        flushed.append(os.readlink(f"/proc/self/fd/{fd}"))
        original(fd)

    monkeypatch.setattr(os, "fsync", flush)
    with pack.open_directory(str(tmp_path)) as fd:
        inventory = pack.inventory_tree(fd, flush=True)
    assert set(inventory.files) == {"nested/aux"}
    assert flushed == [
        str(tmp_path / "nested" / "aux"),
        str(tmp_path / "nested"),
        str(tmp_path),
    ]
