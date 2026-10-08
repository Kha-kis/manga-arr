"""Reserved rescan directories are excluded evidence, never cleanup authority."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from test_rescan_transactions import rescan_env as rescan_env


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
