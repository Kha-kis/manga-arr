"""NFS FILE deletion acceptance; local fault injection, not NFS qualification."""

from __future__ import annotations

import ctypes
import errno
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_volume_file_deletion_journal import deletion_env as deletion_env

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
def test_volume_deletion_and_replay_complete_once_without_noreplace(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    import volume_file_deletion

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    source = Path(str(deletion_env["file_path"]))
    original = source.read_bytes()
    _unsupported_renameat2(monkeypatch, error)

    outcome = volume_file_deletion.replay_volume_file_deletion(reservation.journal_id)

    if outcome == "blocked":
        assert source.read_bytes() == original
        with sqlite3.connect(str(deletion_env["db_path"])) as db:
            assert db.execute(
                "SELECT state FROM volume_file_deletions WHERE id=?",
                (reservation.journal_id,),
            ).fetchone() == ("active",)
    assert outcome == "completed", "deletion claim requires native no-replace rename"
    assert not source.exists()
    assert (
        volume_file_deletion.replay_volume_file_deletion(reservation.journal_id)
        == "terminal"
    )
    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone() == (1,)


@pytest.mark.parametrize("error", UNSUPPORTED)
def test_legacy_flat_deletion_claim_restores_changed_file_without_noreplace(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    import volume_file_deletion

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    journal = volume_file_deletion._load_journal(reservation.journal_id)
    assert journal is not None
    assert journal.target_path == str(deletion_env["file_path"])
    os.rename(journal.target_path, journal.claim_path)
    Path(journal.claim_path).write_bytes(b"changed legacy claim")
    _unsupported_renameat2(monkeypatch, error)

    outcome = volume_file_deletion.replay_volume_file_deletion(reservation.journal_id)

    assert outcome == "blocked"
    if not Path(journal.target_path).exists():
        assert Path(journal.claim_path).read_bytes() == b"changed legacy claim"
    assert Path(journal.target_path).is_file(), "unsupported restoration retained claim"
    assert Path(journal.target_path).read_bytes() == b"changed legacy claim"
    assert not Path(journal.claim_path).exists()


def test_deletion_replay_excludes_overlapping_filesystem_owner(
    deletion_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second replay cannot settle/delete a claim while its first owner is live."""
    import volume_file_deletion

    reservation = volume_file_deletion.reserve_volume_file_deletion(1, 11)
    assert reservation.journal_id is not None
    journal_id = reservation.journal_id
    real_unlink = volume_file_deletion.private_claim.discard_private_regular
    nested_outcomes: list[str] = []
    recreated_claims: list[Path] = []
    entered = False

    def overlapping_unlink(*args) -> None:
        nonlocal entered
        path = args[1].artifact_path
        if entered:
            real_unlink(*args)
            return
        entered = True
        nested_outcomes.append(
            volume_file_deletion.replay_volume_file_deletion(journal_id)
        )
        if not Path(path).exists():
            Path(path).write_bytes(b"unrelated recreated claim")
            recreated_claims.append(Path(path))
        real_unlink(*args)

    monkeypatch.setattr(
        volume_file_deletion.private_claim,
        "discard_private_regular",
        overlapping_unlink,
    )

    volume_file_deletion.replay_volume_file_deletion(journal_id)

    with sqlite3.connect(str(deletion_env["db_path"])) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='file_deleted'"
        ).fetchone() == (1,)
    for path in recreated_claims:
        assert path.is_file(), "first replay deleted an unrelated recreated claim"
        assert path.read_bytes() == b"unrelated recreated claim"
    assert nested_outcomes == ["blocked"], (
        "both replays owned the same filesystem claim"
    )
