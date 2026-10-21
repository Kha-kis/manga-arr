"""Real NULL-token capture/transfer must not discard a retained skip consumer."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import json
from pathlib import Path
from typing import Any

import pytest

from download_identity import (
    DownloadIdentity,
    DownloadProtocol,
    download_identity_key,
    normalize_download_id,
)
from private_pack_claim import PackDirectoryClaim, PackProofError
from test_import_pack_cleanup_durability import (
    _PackEnv,
    _pack_paths,
    _terminal_queue,
    pack_env as pack_env,
)
from test_private_pack_source import _publication


@dataclass(frozen=True)
class Captured:
    queue_id: int
    identity: DownloadIdentity
    source: Path
    artifact: Path
    journal: dict[str, Any]
    payload: bytes


def _capture(
    env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    protocol: DownloadProtocol,
    fallback: bool,
    private_legacy: bool = False,
) -> Captured:
    import import_pack_cleanup as cleanup
    import shared

    identity = DownloadIdentity(7, protocol, "Captured-Pack")
    queue_id = _terminal_queue(
        env["db_path"], identity.download_id, download_client_id=7, protocol=protocol
    )
    canonical, _ = _pack_paths(
        identity.download_id, download_client_id=7, protocol=protocol
    )
    canonical.mkdir(parents=True, mode=0o700)
    source = canonical / "page.cbz"
    payload = b"existing independently captured legacy pack"
    source.write_bytes(payload)
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO import_queue_files(queue_id,filename,src_path,status)"
            " VALUES(?,'page.cbz',?,'failed')",
            (queue_id, str(source)),
        )

    def unsupported(_source: str, _destination: str) -> None:
        raise OSError(errno.EOPNOTSUPP, "exercise real private-claim fallback")

    if private_legacy:
        from private_pack_claim import inventory_tree, open_directory

        with open_directory(str(canonical)) as descriptor:
            fingerprint = inventory_tree(descriptor).files[source.name]
        with shared.get_db() as db:
            queue = dict(
                db.execute(
                    "SELECT * FROM import_queue WHERE id=?", (queue_id,)
                ).fetchone()
            )
            file = dict(
                db.execute(
                    "SELECT * FROM import_queue_files WHERE queue_id=?", (queue_id,)
                ).fetchone()
            )
        queue["lease_owner"] = "completed-historical-publication"
        file["dst_path"] = str(env["library"] / source.name)
        publication_id = _publication(env, queue, file, {}, fingerprint)
        private = Path(str(canonical) + ".cleanup-historical")
        assert cleanup._detach_directory(str(canonical), str(private)) == "detached"
        with shared.get_db() as db:
            db.execute(
                "UPDATE import_publications SET state='finalized' WHERE id=?",
                (publication_id,),
            )
            cleanup._track_detached_tombstone(
                db,
                download_identity_key=download_identity_key(identity),
                download_client_id=7,
                protocol=protocol,
                normalized_download_id=normalize_download_id(
                    identity.download_id, protocol
                ),
                download_id=identity.download_id,
                queue_id=queue_id,
                publication_id=publication_id,
                pack_path=str(canonical),
                tombstone_path=str(private),
            )

    with monkeypatch.context() as pause:
        if fallback:
            pause.setattr(cleanup, "_rename_noreplace", unsupported)
        # Pause only after production capture + SQL authority transfer, before GC.
        if private_legacy:

            def crash_after_capture(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("power loss after durable private tombstone capture")

            pause.setattr(cleanup, "_discard_pack_claim", crash_after_capture)
            with pytest.raises(RuntimeError, match="durable private tombstone capture"):
                cleanup.recover_pack_cleanup_state()
        else:
            pause.setattr(cleanup, "_remove_tracked_tombstone", lambda *_a, **_k: False)
            assert not cleanup.cleanup_terminal_pack_staging(
                queue_id, identity.download_id, download_client_id=7, protocol=protocol
            )
    with shared.get_db() as db:
        row = db.execute(
            "SELECT * FROM import_pack_cleanup_tombstones WHERE queue_id=?", (queue_id,)
        ).fetchone()
        assert row is not None
        journal = dict(row)
        assert (
            db.execute(
                "SELECT 1 FROM import_pack_cleanup_reservations WHERE queue_id=?",
                (queue_id,),
            ).fetchone()
            is None
        )
    claim = PackDirectoryClaim.from_json(journal["carrier_json"])
    artifact = Path(journal["tombstone_path"]) / source.name
    assert claim.artifact_owner_token is None
    assert claim.source_directory.marker_fingerprint is None
    expected_origin = (
        str(canonical) + ".cleanup-historical" if private_legacy else str(canonical)
    )
    assert claim.source_directory.path == expected_origin
    assert claim.carrier.origin_path == expected_origin
    assert claim.carrier.binding.domain == "pack"
    assert claim.carrier.binding.purpose == "pack_cleanup"
    assert claim.carrier.binding.file_id is None
    assert journal["download_identity_key"] == download_identity_key(identity)
    assert (
        Path(journal["tombstone_path"]) == Path(claim.carrier.carrier_path) / "artifact"
    )
    assert not source.exists(), "the original name was already moved before recovery"
    assert artifact.read_bytes() == payload
    (env["tmp_path"] / "capture-proof.json").write_text(
        json.dumps(
            {"original_source": str(source), "artifact": str(artifact), **journal},
            indent=2,
        )
    )
    return Captured(queue_id, identity, source, artifact, journal, payload)


@pytest.mark.parametrize("protocol", ["torrent", "nzb"])
@pytest.mark.parametrize("fallback", [False, True], ids=["native", "private-fallback"])
@pytest.mark.parametrize(
    "private_legacy", [False, True], ids=["canonical-proof", "private-proof"]
)
def test_null_token_transferred_carrier_survives_retained_consumer_recovery(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    protocol: DownloadProtocol,
    fallback: bool,
    private_legacy: bool,
) -> None:
    import import_pack_cleanup as cleanup
    import shared

    case = _capture(pack_env, monkeypatch, protocol, fallback, private_legacy)
    with shared.get_db() as db:
        db.execute(
            "UPDATE import_queue_files SET status='skipped' WHERE queue_id=?",
            (case.queue_id,),
        )
        before = [tuple(row) for row in db.execute("SELECT * FROM import_queue_files")]
        publications = [
            tuple(row) for row in db.execute("SELECT * FROM import_publications")
        ]
    result = cleanup.recover_pack_cleanup_state()
    assert case.artifact.is_file(), (
        "recovery deleted the retained consumer's captured carrier"
    )
    assert case.artifact.read_bytes() == case.payload
    assert result.tombstones_removed == 0 and result.tombstones_retained == 1
    assert not case.source.exists(), (
        "retention is not authority to restore a moved name"
    )
    with shared.get_db() as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM import_queue_files")
        ] == before
        assert [
            tuple(row) for row in db.execute("SELECT * FROM import_publications")
        ] == publications
        assert db.execute("SELECT 1 FROM history").fetchone() is None
        # Actual dismissal ends the structural source consumer; ordinary GC still works.
        db.execute(
            "UPDATE import_queue SET status='skipped' WHERE id=?", (case.queue_id,)
        )
    settled = cleanup.recover_pack_cleanup_state()
    assert settled.tombstones_removed == 1 and settled.tombstones_retained == 0
    assert not case.artifact.parent.exists()


@pytest.mark.parametrize("status", ["failed", "imported", "skipped"])
def test_null_token_ordinary_terminal_consumer_does_not_gain_a_veto(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    import import_pack_cleanup as cleanup
    import shared

    case = _capture(pack_env, monkeypatch, "torrent", True)
    with shared.get_db() as db:
        db.execute(
            "UPDATE import_queue SET status=? WHERE id=?", (status, case.queue_id)
        )
    result = cleanup.recover_pack_cleanup_state()
    assert result.tombstones_removed == 1
    assert not case.artifact.parent.exists()


@pytest.mark.parametrize(
    "boundary",
    [
        "row_owner",
        "row_protocol",
        "row_id",
        "row_key",
        "row_normalized",
        "row_pack",
        "carrier_path",
        "source_path",
        "token",
        "inventory",
        "child_source",
        "mixed_child",
        "null_claim",
        "malformed_claim",
    ],
)
def test_null_token_veto_requires_exact_identity_target_carrier_and_inventory(
    pack_env: _PackEnv, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    import import_pack_cleanup as cleanup
    import shared

    case = _capture(pack_env, monkeypatch, "torrent", True)
    with shared.get_db() as db:
        db.execute(
            "UPDATE import_queue_files SET status='skipped' WHERE queue_id=?",
            (case.queue_id,),
        )
        columns = {
            "row_owner": ("download_client_id", 8),
            "row_protocol": ("protocol", "nzb"),
            "row_id": ("download_id", "unrelated"),
            "row_key": ("download_identity_key", "unrelated"),
            "row_normalized": ("normalized_download_id", "unrelated"),
            "row_pack": ("pack_path", case.journal["pack_path"] + "-unrelated"),
            "carrier_path": (
                "tombstone_path",
                case.journal["tombstone_path"] + "-unrelated",
            ),
            "null_claim": ("carrier_json", None),
            "malformed_claim": ("carrier_json", "{"),
        }
        if boundary in columns:
            column, value = columns[boundary]
            db.execute(
                f"UPDATE import_pack_cleanup_tombstones SET {column}=?", (value,)
            )
        elif boundary in ("source_path", "token", "inventory"):
            value = json.loads(case.journal["carrier_json"])
            if boundary == "source_path":
                origin = str(case.source.parent) + "-unrelated"
                value["source_directory"]["path"] = origin
                value["carrier"]["origin_path"] = origin
            elif boundary == "token":
                value["artifact_owner_token"] = "foreign-token"
            else:
                value["inventory"]["files"] = {}
            db.execute(
                "UPDATE import_pack_cleanup_tombstones SET carrier_json=?",
                (json.dumps(value),),
            )
        elif boundary == "child_source":
            db.execute(
                "UPDATE import_queue_files SET src_path=? WHERE queue_id=?",
                (str(case.source) + "-unrelated", case.queue_id),
            )
        else:
            db.execute(
                "INSERT INTO import_queue_files(queue_id,filename,src_path,status) VALUES(?,'other',?,'failed')",
                (case.queue_id, str(case.source)),
            )
        if boundary == "malformed_claim":
            with pytest.raises(PackProofError):
                cleanup._retained_skipped_pack_uses_source(
                    db, queue_id=case.queue_id, identity=case.identity
                )
        else:
            assert not cleanup._retained_skipped_pack_uses_source(
                db, queue_id=case.queue_id, identity=case.identity
            )
    assert case.artifact.read_bytes() == case.payload
