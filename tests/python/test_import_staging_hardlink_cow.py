"""Focused regression coverage for hardlink staging copy-on-write."""

from __future__ import annotations

import asyncio
import hashlib
import os
import zipfile
from types import SimpleNamespace

from import_staging import _ImportStaging, _stage_files


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_hardlink_enrichment_copies_on_write_without_mutating_source(
    tmp_path,
) -> None:
    source_path = tmp_path / "downloads" / "Series v01.cbz"
    source_path.parent.mkdir()
    with zipfile.ZipFile(source_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("001.png", b"page")

    destination_dir = tmp_path / "library" / "Series"
    destination_dir.mkdir(parents=True)
    final_path = destination_dir / source_path.name
    staging = _ImportStaging(
        str(destination_dir),
        queue_id=17,
        import_mode="hardlink",
    )
    file_plan = SimpleNamespace(
        file_id=1,
        plan_status="ready",
        src_path=str(source_path),
        dst_path=str(final_path),
        file_type="volume",
        proposed_chap=None,
        proposed_vol=1.0,
    )
    plan = SimpleNamespace(
        files=[file_plan],
        import_mode="hardlink",
        series={"title": "Series", "language": "en"},
        series_tags=["owned-test"],
    )
    source_inode = os.stat(source_path).st_ino
    source_hash = _sha256(str(source_path))

    outcomes = asyncio.run(_stage_files(plan, staging))

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.ok is True
    assert os.stat(source_path).st_ino == source_inode
    assert _sha256(str(source_path)) == source_hash
    assert os.stat(outcome.stage_path).st_ino != source_inode
    assert staging.records[0].stage_path == outcome.stage_path
    with zipfile.ZipFile(outcome.stage_path) as archive:
        assert "ComicInfo.xml" in archive.namelist()
        assert b"<Series>Series</Series>" in archive.read("ComicInfo.xml")


def test_hardlink_enrichment_copies_old_inode_when_source_path_is_replaced(
    tmp_path,
    monkeypatch,
) -> None:
    source_path = tmp_path / "downloads" / "Series v03.cbz"
    source_path.parent.mkdir()
    with zipfile.ZipFile(source_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("001.png", b"old page")

    old_source_hardlink = source_path.with_name("old-source-hardlink.cbz")
    os.link(source_path, old_source_hardlink)
    old_inode = os.stat(source_path).st_ino
    old_hash = _sha256(str(old_source_hardlink))

    replacement_path = source_path.with_name("replacement.cbz")
    with zipfile.ZipFile(replacement_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("001.png", b"new page")
    replacement_hash = _sha256(str(replacement_path))

    destination_dir = tmp_path / "library" / "Series"
    destination_dir.mkdir(parents=True)
    staging = _ImportStaging(
        str(destination_dir),
        queue_id=19,
        import_mode="hardlink",
    )
    file_plan = SimpleNamespace(
        file_id=3,
        plan_status="ready",
        src_path=str(source_path),
        dst_path=str(destination_dir / source_path.name),
        file_type="volume",
        proposed_chap=None,
        proposed_vol=3.0,
    )
    plan = SimpleNamespace(
        files=[file_plan],
        import_mode="hardlink",
        series={"title": "Series", "language": "en"},
        series_tags=["owned-test"],
    )
    prepare_for_mutation = staging.prepare_for_mutation

    def replace_source_then_prepare(stage_path: str) -> str:
        assert os.stat(stage_path).st_ino == old_inode
        os.replace(replacement_path, source_path)
        return prepare_for_mutation(stage_path)

    monkeypatch.setattr(
        staging,
        "prepare_for_mutation",
        replace_source_then_prepare,
    )

    outcomes = asyncio.run(_stage_files(plan, staging))

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.ok is True
    assert os.stat(old_source_hardlink).st_ino == old_inode
    assert _sha256(str(old_source_hardlink)) == old_hash
    assert os.stat(source_path).st_ino != old_inode
    assert _sha256(str(source_path)) == replacement_hash
    staged_stat = os.stat(outcome.stage_path)
    assert staged_stat.st_ino not in {
        old_inode,
        os.stat(source_path).st_ino,
    }
    assert staged_stat.st_nlink == 1
    with zipfile.ZipFile(outcome.stage_path) as archive:
        assert "ComicInfo.xml" in archive.namelist()
        assert b"<Series>Series</Series>" in archive.read("ComicInfo.xml")


def test_hardlink_without_enrichment_keeps_source_inode(tmp_path) -> None:
    source_path = tmp_path / "downloads" / "Series v02.cbz"
    source_path.parent.mkdir()
    with zipfile.ZipFile(source_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("001.png", b"page")

    destination_dir = tmp_path / "library" / "Series"
    destination_dir.mkdir(parents=True)
    staging = _ImportStaging(
        str(destination_dir),
        queue_id=18,
        import_mode="hardlink",
    )
    file_plan = SimpleNamespace(
        file_id=2,
        plan_status="ready",
        src_path=str(source_path),
        dst_path=str(destination_dir / source_path.name),
        file_type="volume",
        proposed_chap=None,
        proposed_vol=2.0,
    )
    plan = SimpleNamespace(
        files=[file_plan],
        import_mode="hardlink",
        series=None,
        series_tags=[],
    )

    outcomes = asyncio.run(_stage_files(plan, staging))

    assert outcomes[0].ok is True
    assert os.stat(outcomes[0].stage_path).st_ino == os.stat(source_path).st_ino
