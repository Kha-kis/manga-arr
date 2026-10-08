"""Reserved rescan directories are excluded evidence, never cleanup authority."""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

from test_rescan_transactions import rescan_env as rescan_env
from test_import_publication_journal import _seed_queue, journal_env as journal_env


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


@pytest.mark.parametrize(
    "private_name",
    [".mangarr-claims", ".mangarr-claim-unregistered", ".mangarr-rescan-unregistered"],
)
def test_adoption_shared_inventory_excludes_private_files_and_preserves_provider_floor(
    rescan_env: dict[str, Any], private_name: str
) -> None:
    import library_scan

    target = cast(Path, rescan_env["library_root"]) / "Adopt Me"
    target.mkdir()
    public = target / "Adopt Me v01.cbz"
    public.write_bytes(b"public-original")
    private = target / "nested" / private_name
    private.mkdir(parents=True)
    artifact = private / "Adopt Me v08.cbz"
    artifact.write_bytes(b"private-original")
    proof = private / "proof.json"
    proof.write_bytes(b'{"unregistered":true}')

    result = library_scan.adopt_unmapped_folder(
        1,
        str(target.resolve()),
        total_volumes=2,
        metadata_source="anilist",
    )

    assert result.ok
    assert result.payload is not None
    series_id = result.payload["series"]["id"]
    with sqlite3.connect(str(rescan_env["db_path"])) as db:
        volumes = db.execute(
            "SELECT volume_num,status,import_path FROM volumes"
            " WHERE series_id=? ORDER BY volume_num",
            (series_id,),
        ).fetchall()
        count = db.execute(
            "SELECT total_volumes,vol_count_source FROM series WHERE id=?",
            (series_id,),
        ).fetchone()
    assert result.payload["rescan"]["created"] == 0
    assert result.payload["rescan"]["recovered"] == 1
    assert volumes == [(1.0, "downloaded", str(public)), (2.0, "wanted", None)]
    assert count == (2, "anilist")
    assert public.read_bytes() == b"public-original"
    assert artifact.read_bytes() == b"private-original"
    assert proof.read_bytes() == b'{"unregistered":true}'


@pytest.mark.parametrize(
    "ordinary_name",
    [
        ".extras",
        ".mangarr-claims-backup",
        ".mangarr-claim",
        ".mangarr-rescan",
        "mangarr-claims",
        ".mangarr-publication",
        ".mangarr-staging",
        "mangarr-publication-1-token",
        "mangarr-staging-1-token",
    ],
)
def test_inventory_keeps_nonreserved_hidden_and_similarly_named_directories(
    rescan_env: dict[str, Any], ordinary_name: str
) -> None:
    import rescan

    ordinary = cast(Path, rescan_env["series_dir"]) / ordinary_name
    ordinary.mkdir()
    source = ordinary / "Race Manga v03.cbz"
    source.write_bytes(b"public-original")

    result = rescan.rescan_series_folder(7)

    with sqlite3.connect(str(rescan_env["db_path"])) as db:
        volume = db.execute(
            "SELECT volume_num,status,import_path FROM volumes WHERE series_id=7"
        ).fetchone()
        count = db.execute(
            "SELECT total_volumes,vol_count_source FROM series WHERE id=7"
        ).fetchone()
    assert result["created"] == 1
    assert volume == (3.0, "downloaded", str(source))
    assert count == (3, "local")
    assert source.read_bytes() == b"public-original"


@pytest.mark.parametrize(
    ("folder_name", "reserved"),
    [
        (".mangarr-claim-X", True),
        (".mangarr-claims", True),
        (".mangarr-rescan-X", True),
        (".ordinary-hidden-manga", False),
        (".mangarr-claims-backup", False),
    ],
)
def test_direct_adoption_initial_directory_excludes_only_reserved_content(
    rescan_env: dict[str, Any], folder_name: str, reserved: bool
) -> None:
    import library_scan

    target = cast(Path, rescan_env["library_root"]) / folder_name
    target.mkdir()
    artifact = target / "Initial Folder Manga v08.cbz"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("001.jpg", b"real archive page content")
    before = artifact.read_bytes()

    result = library_scan.adopt_unmapped_folder(
        1, str(target), title="Initial Folder Manga"
    )

    assert result.ok
    assert result.payload is not None
    series_id = result.payload["series"]["id"]
    with sqlite3.connect(str(rescan_env["db_path"])) as db:
        volumes = db.execute(
            "SELECT volume_num,status,import_path FROM volumes WHERE series_id=?",
            (series_id,),
        ).fetchall()
        count = db.execute(
            "SELECT total_volumes FROM series WHERE id=?", (series_id,)
        ).fetchone()
    assert count == (None,)
    if reserved:
        assert volumes == [], "initial private directory became library evidence"
        assert result.payload["rescan"]["found"] == 0
        assert artifact.read_bytes() == before
    else:
        assert volumes == [(8.0, "downloaded", str(artifact))]
        with zipfile.ZipFile(artifact) as archive:
            assert archive.read("001.jpg") == b"real archive page content"


@pytest.mark.parametrize(
    "reserved_name", [".mangarr-claims", ".mangarr-claim-X", ".mangarr-rescan-X"]
)
def test_inventory_initial_path_below_reserved_directory_is_empty(
    rescan_env: dict[str, Any], reserved_name: str
) -> None:
    import rescan

    target = cast(Path, rescan_env["library_root"]) / reserved_name / "carrier"
    target.mkdir(parents=True)
    artifact = target / "Private Manga v08.cbz"
    artifact.write_bytes(b"private-original")
    proof = target / "owner.json"
    proof.write_bytes(b'{"unregistered":true}')
    snapshot = rescan.SeriesRescanSnapshot({}, str(target), (), (), (), {})

    inventory = rescan.build_filesystem_inventory(snapshot)

    assert inventory.on_disk == frozenset()
    assert not inventory.any_library_files
    assert artifact.read_bytes() == b"private-original"
    assert proof.read_bytes() == b'{"unregistered":true}'


@pytest.mark.parametrize(
    ("target_name", "alias_name", "reserved"),
    [
        (".mangarr-claims", "ordinary-alias", True),
        ("ordinary-directory", ".mangarr-claims", True),
        ("ordinary-directory", "ordinary-alias", False),
    ],
)
def test_inventory_initial_symlink_excludes_only_reserved_boundaries(
    rescan_env: dict[str, Any],
    target_name: str,
    alias_name: str,
    reserved: bool,
) -> None:
    import rescan

    library_root = cast(Path, rescan_env["library_root"])
    private = library_root / target_name
    private.mkdir()
    artifact = private / "Private Manga v08.cbz"
    artifact.write_bytes(b"private-original")
    alias = library_root / alias_name
    alias.symlink_to(private, target_is_directory=True)
    snapshot = rescan.SeriesRescanSnapshot({}, str(alias), (), (), (), {})

    inventory = rescan.build_filesystem_inventory(snapshot)

    assert inventory.on_disk == (frozenset() if reserved else frozenset({8.0}))
    assert inventory.any_library_files is not reserved
    assert artifact.read_bytes() == b"private-original"
    assert alias.is_symlink()


def _stage_private_import(
    destination: Path, source: Path, producer: str
) -> tuple[Path, Path, bytes]:
    """Retained unregistered flat layouts, not current journal-owned carriers."""
    from import_publication import deterministic_staging_dir
    from import_staging import _ImportStaging

    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.jpg", b"private import page content")
    staging_dir = None
    if producer != "staging":
        staging_dir = deterministic_staging_dir(
            str(destination),
            1,
            "inventory-owner" if producer == "publication" else None,
        )
    staging = _ImportStaging(
        str(destination),
        1,
        "copy",
        staging_dir=staging_dir,
        journal_owned=False,
    )
    artifact = Path(
        staging.stage(str(source), str(destination / "Private Manga v08.cbz"))
    )
    witness = Path(staging.staging_dir) / "retained.json"
    witness.write_bytes(b'{"inventory_must_not_mutate":true}')
    return artifact, witness, source.read_bytes()


def test_current_pinned_import_stage_is_not_inventory_evidence(journal_env) -> None:
    import rescan
    import shared
    from import_lease import claim_import_queue_row, IMPORT_LEASE_SECONDS
    from import_plan import _plan_import
    from import_publication import initialize_publication_filesystem
    from import_staging import _ImportStaging

    queue_id, _, sources, _ = _seed_queue(journal_env, file_count=1)
    original = sources[0].read_bytes()
    owner = "current-inventory-owner"
    with shared.get_db() as db:
        assert claim_import_queue_row(db, queue_id, owner)
        plan = _plan_import(
            db, queue_id, owner, {}, {}, set(), "copy",
            lease_seconds=IMPORT_LEASE_SECONDS,
        )
    assert plan is not None
    stage, _ = initialize_publication_filesystem(plan, owner)
    with sqlite3.connect(journal_env["db_path"]) as db:
        row = db.execute("SELECT id FROM import_publications").fetchone()
    assert row is not None
    staging = _ImportStaging(
        plan.dst_dir, queue_id, "copy", staging_dir=stage,
        journal_owned=True, publication_id=row[0], owner_token=owner,
    )
    outcome = staging.stage_one(plan, plan.files[0])
    assert outcome.ok
    artifact = Path(outcome.stage_path)
    staged = artifact.read_bytes()
    with zipfile.ZipFile(artifact) as archive:
        assert "ComicInfo.xml" in archive.namelist()

    for entry in (stage, plan.dst_dir):
        snapshot = rescan.SeriesRescanSnapshot({}, entry, (), (), (), {})
        inventory = rescan.build_filesystem_inventory(snapshot)
        assert inventory.on_disk == frozenset()
        assert not inventory.any_library_files
    assert sources[0].read_bytes() == original
    assert artifact.read_bytes() == staged


@pytest.mark.parametrize("producer", ["publication", "publication_legacy", "staging"])
@pytest.mark.parametrize("workflow", ["rescan", "adoption"])
def test_actual_import_staging_is_not_rescan_or_adoption_evidence(
    rescan_env: dict[str, Any], producer: str, workflow: str
) -> None:
    import library_scan
    import rescan

    destination = cast(Path, rescan_env["series_dir"])
    if workflow == "adoption":
        destination = cast(Path, rescan_env["library_root"]) / "Private Import Adoption"
        destination.mkdir()
    source = cast(Path, rescan_env["library_root"]).parent / "incoming.cbz"
    artifact, witness, before = _stage_private_import(destination, source, producer)
    public = destination / "Public Manga v01.cbz"
    public.write_bytes(b"public-original")

    if workflow == "rescan":
        series_id = 7
        result = rescan.rescan_series_folder(series_id)
        expected_count = (1, "local")
    else:
        adoption = library_scan.adopt_unmapped_folder(
            1, str(destination), total_volumes=2, metadata_source="anilist"
        )
        assert adoption.ok
        assert adoption.payload is not None
        series_id = adoption.payload["series"]["id"]
        result = adoption.payload["rescan"]
        expected_count = (2, "anilist")

    with sqlite3.connect(str(rescan_env["db_path"])) as db:
        volumes = db.execute(
            "SELECT volume_num,status,import_path FROM volumes"
            " WHERE series_id=? ORDER BY volume_num",
            (series_id,),
        ).fetchall()
        count = db.execute(
            "SELECT total_volumes,vol_count_source FROM series WHERE id=?", (series_id,)
        ).fetchone()
    expected_volumes: list[tuple[float, str, str | None]] = [
        (1.0, "downloaded", str(public))
    ]
    if workflow == "adoption":
        expected_volumes.append((2.0, "wanted", None))
    assert volumes == expected_volumes, "actual staged filename became library evidence"
    assert count == expected_count
    assert result["found"] == 1
    assert not (destination / artifact.name).exists()
    assert source.read_bytes() == before
    assert artifact.read_bytes() == before
    assert witness.read_bytes() == b'{"inventory_must_not_mutate":true}'
    assert public.read_bytes() == b"public-original"


@pytest.mark.parametrize("producer", ["publication", "publication_legacy", "staging"])
@pytest.mark.parametrize("boundary", ["root_adoption", "descendant", "resolved_alias"])
def test_actual_import_staging_initial_path_is_not_evidence(
    rescan_env: dict[str, Any], producer: str, boundary: str
) -> None:
    import library_scan
    import rescan

    library_root = cast(Path, rescan_env["library_root"])
    source = library_root.parent / "incoming.cbz"
    artifact, witness, before = _stage_private_import(library_root, source, producer)
    target = artifact.parent
    if boundary == "descendant":
        nested = target / "nested"
        nested.mkdir()
        artifact = artifact.rename(nested / artifact.name)
        target = nested
    elif boundary == "resolved_alias":
        alias = library_root / "ordinary-import-alias"
        alias.symlink_to(target, target_is_directory=True)
        target = alias

    if boundary == "root_adoption":
        adoption = library_scan.adopt_unmapped_folder(
            1, str(target), title="Private Import Adoption"
        )
        assert adoption.ok
        assert adoption.payload is not None
        with sqlite3.connect(str(rescan_env["db_path"])) as db:
            volumes = db.execute(
                "SELECT volume_num FROM volumes WHERE series_id=?",
                (adoption.payload["series"]["id"],),
            ).fetchall()
        assert volumes == []
        assert adoption.payload["rescan"]["found"] == 0
    else:
        snapshot = rescan.SeriesRescanSnapshot({}, str(target), (), (), (), {})
        inventory = rescan.build_filesystem_inventory(snapshot)
        assert inventory.on_disk == frozenset()
        assert not inventory.any_library_files
    assert artifact.read_bytes() == before
    assert source.read_bytes() == before
    assert witness.read_bytes() == b'{"inventory_must_not_mutate":true}'
