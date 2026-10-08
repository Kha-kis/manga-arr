"""Open RED release gate: post-admission ownership changes need compensation."""

import asyncio
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import grab_core
import import_execute
import import_publication
import shared
from clients import GrabResult
from rescan import _series_library_dir
from routers import series_ as routes
from test_grab_monitoring_380 import (
    commit_pack_env,
    ownership_env,
    queue_pack_files,
    release,
    _archive,
)


@pytest.fixture(autouse=True)
def _restore_prior_secret_cipher(monkeypatch):
    """The imported legacy owner fixture assigns this cache directly."""
    import security

    monkeypatch.setattr(security, "_SECRET_CIPHER", security._SECRET_CIPHER)


@pytest.mark.parametrize("protocol", ["nzb", "torrent"])
def test_real_reset_after_publication_admission_restores_original_bytes(
    commit_pack_env, monkeypatch, protocol
):
    paths = commit_pack_env
    assert isinstance(grab_core.grab_url, AsyncMock)
    grab_core.grab_url.return_value = GrabResult(
        True,
        "sabnzbd" if protocol == "nzb" else "qbittorrent",
        "NZO-pack" if protocol == "nzb" else "pack-hash",
        True,
        7,
    )
    item = release("Test Series v01-v03")
    item["protocol"] = protocol
    assert asyncio.run(grab_core.grab_item(item, 1))
    _archive(paths["downloads"] / "Test Series v01.cbz")
    qid = queue_pack_files(paths, protocol)
    with shared.get_db() as db:
        filename = db.execute(
            "SELECT filename FROM import_queue_files WHERE queue_id=?", (qid,)
        ).fetchone()[0]
        directory = _series_library_dir(db, 1)
        assert directory is not None
        destination = Path(directory) / filename
        db.execute("UPDATE volumes SET import_path=? WHERE id=1", (str(destination),))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("001.jpg", b"original owned canonical bytes")
    original = destination.read_bytes()
    observations = []
    real_rename = import_publication._rename_noreplace

    def rename(source, target):
        result = real_rename(source, target)
        if source == str(destination):
            asyncio.run(routes.reset_volume_to_wanted(1, 1))
            with shared.get_db() as db:
                observations.append(
                    dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
                )
        return result

    monkeypatch.setattr(import_publication, "_rename_noreplace", rename)
    asyncio.run(import_execute._execute_import(qid))
    assert observations, "actual reset route was not exercised after admission"
    assert destination.read_bytes() == original
    with shared.get_db() as db:
        assert (
            dict(db.execute("SELECT * FROM volumes WHERE id=1").fetchone())
            == observations[-1]
        )
