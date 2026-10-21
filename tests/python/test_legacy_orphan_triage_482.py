"""Legacy evidence characterization and orphan safety, without inferred repair."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import closing, contextmanager
import json
import sqlite3
from typing import Any

import httpx
import pytest

from download_identity import DownloadIdentity, DownloadProtocol
from import_lease import ImportQueueStatus
from test_import_skip_settlement_482 import (
    Pack,
    SOURCE,
    _assert_guid_dedup,
    _commit_skip,
    _poll_removed,
    _queue,
    _snapshot,
    pack as pack,
)


@pytest.fixture(autouse=True)
def isolated_event_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    import events

    monkeypatch.setattr(events, "_LOG_DEDUP_LAST", {})


def _binding_evidence_snapshot() -> dict[str, list[dict[str, Any]]]:
    import shared

    with shared.get_db() as db:
        return {
            table: [
                dict(row)
                for row in db.execute(f"SELECT * FROM {table} ORDER BY {order_column}")
            ]
            for table, order_column in (
                ("seen", "torrent_url"),
                ("import_queue", "id"),
                ("import_queue_files", "id"),
                ("history", "id"),
            )
        }


def test_different_id_claim_keeps_same_poll_source_refusal(
    pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier acquisition must not consume the later claim's refusal proof."""
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("DELETE FROM chapters WHERE id != 30")
        db.execute("UPDATE chapters SET download_id='later-id' WHERE id=30")
    claims = _snapshot()
    evidence = _binding_evidence_snapshot()
    _poll_removed(pack, monkeypatch)
    # Both bindings survive: choosing the seen binding would erase durable proof.
    assert _snapshot() == claims
    assert _binding_evidence_snapshot() == evidence


@pytest.mark.parametrize(
    ("difference", "evidence_table", "label", "local"),
    [
        ("id", "chapters", None, False),
        ("id", "volumes", "", True),
        ("owner", "chapters", "opposite", True),
        ("owner", "volumes", None, False),
        ("protocol", "chapters", "", True),
        ("protocol", "volumes", "opposite", False),
    ],
)
def test_current_source_bindings_survive_polls_and_receipt_expiry(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    difference: str,
    evidence_table: str,
    label: str | None,
    local: bool,
) -> None:
    import import_discovery
    import shared

    queue_id = _queue(pack)
    other_protocol: DownloadProtocol = (
        "nzb" if pack.protocol == "torrent" else "torrent"
    )
    alternate_protocol = other_protocol if difference == "protocol" else pack.protocol
    alternate_owner = 8 if difference == "owner" else 7
    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("DELETE FROM chapters WHERE id != 30")
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
        row_id = 2 if evidence_table == "volumes" else 30
        db.execute(
            f"UPDATE {evidence_table} SET download_client_id=?,protocol=?,"
            " download_id=?,client=?,import_path=? WHERE id=?",
            (
                alternate_owner,
                alternate_protocol,
                "later-id" if difference == "id" else pack.download_id,
                ("sabnzbd" if pack.client_type == "qbittorrent" else "qbittorrent")
                if label == "opposite"
                else label,
                str(pack.original) if local else None,
                row_id,
            ),
        )
    claims = _snapshot()
    evidence = _binding_evidence_snapshot()
    for poll in range(3):
        _poll_removed(pack, monkeypatch)
        # Exercise the other namespace too, even when its client label is unusable.
        import_discovery._cleanup_missing_downloads(
            set(),
            download_client_id=alternate_owner,
            protocol=alternate_protocol,
            client_type="qbittorrent" if alternate_protocol == "torrent" else "sabnzbd",
            include_legacy_ownerless=False,
        )
        assert _snapshot() == claims
        assert _binding_evidence_snapshot() == evidence
        if poll == 0:
            with shared.get_db() as db:
                db.execute("DELETE FROM seen")
            evidence["seen"] = []
    with shared.get_db() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM events WHERE message="
                "'Orphan cleanup deferred: conflicting claim bindings'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("difference", ["owner", "protocol", "series", "id"])
def test_independent_binding_namespaces_remain_retryable(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, difference: str
) -> None:
    import import_discovery
    import shared

    other_protocol: DownloadProtocol = (
        "nzb" if pack.protocol == "torrent" else "torrent"
    )
    alternate_protocol = other_protocol if difference == "protocol" else pack.protocol
    alternate_owner = 8 if difference == "owner" else 7
    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("DELETE FROM chapters WHERE id != 30")
        db.execute(
            "UPDATE chapters SET torrent_url=?,download_client_id=?,protocol=?,"
            " series_id=?,download_id=? WHERE id=30",
            (
                SOURCE if difference == "series" else "https://independent.invalid",
                alternate_owner,
                alternate_protocol,
                2 if difference == "series" else 1,
                "independent-id" if difference == "id" else pack.download_id,
            ),
        )
    _poll_removed(pack, monkeypatch)
    import_discovery._cleanup_missing_downloads(
        set(),
        download_client_id=alternate_owner,
        protocol=alternate_protocol,
        client_type="qbittorrent" if alternate_protocol == "torrent" else "sabnzbd",
        include_legacy_ownerless=False,
    )
    with shared.get_db() as db:
        assert tuple(
            db.execute("SELECT status,download_id FROM volumes WHERE id=2").fetchone()
        ) == ("wanted", None)
        assert tuple(
            db.execute("SELECT status,download_id FROM chapters WHERE id=30").fetchone()
        ) == ("wanted", None)
        assert db.execute("SELECT 1 FROM seen").fetchone() is None
        assert (
            db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='grab_failed'"
            ).fetchone()[0]
            == 2
        )
    after = _snapshot(), _binding_evidence_snapshot()
    _poll_removed(pack, monkeypatch)
    assert (_snapshot(), _binding_evidence_snapshot()) == after


@pytest.mark.parametrize("same_source", [False, True])
def test_binding_identity_uses_protocol_specific_id_case(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, same_source: bool
) -> None:
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("DELETE FROM chapters WHERE id != 30")
        db.execute(
            "UPDATE chapters SET download_id=?,torrent_url=?,protocol=? WHERE id=30",
            (
                pack.download_id.swapcase(),
                SOURCE if same_source else "https://independent.invalid",
                " usenet " if pack.protocol == "nzb" else " TORRENT ",
            ),
        )
    claims = _snapshot()
    refused = (pack.protocol == "torrent") != same_source
    _poll_removed(pack, monkeypatch)
    if refused:
        assert _snapshot() == claims
        with shared.get_db() as db:
            db.execute("DELETE FROM seen")
        _poll_removed(pack, monkeypatch)
        _poll_removed(pack, monkeypatch)
        assert _snapshot() == claims
        assert _binding_evidence_snapshot()["history"] == []
    else:
        with shared.get_db() as db:
            assert (
                db.execute("SELECT status FROM volumes WHERE id=2").fetchone()[0]
                == "wanted"
            )
            assert (
                db.execute("SELECT status FROM chapters WHERE id=30").fetchone()[0]
                == "wanted"
            )
            assert db.execute("SELECT 1 FROM seen").fetchone() is None
            assert db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='grab_failed'"
            ).fetchone()[0] == (1 if same_source else 2)


@pytest.mark.parametrize("chapter_id", [20, 30], ids=["mapped", "loose"])
@pytest.mark.parametrize("expire_seen", [False, True])
def test_current_source_fork_refuses_repeated_polls_even_after_seen_expiry(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, chapter_id: int, expire_seen: bool
) -> None:
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("DELETE FROM chapters WHERE id != ?", (chapter_id,))
        db.execute("UPDATE chapters SET torrent_url='https://successor.invalid'")
        seen_before = dict(
            db.execute("SELECT * FROM seen WHERE torrent_url=?", (SOURCE,)).fetchone()
        )
    before = _snapshot()
    _poll_removed(pack, monkeypatch)
    assert _snapshot() == before
    with shared.get_db() as db:
        assert (
            dict(
                db.execute(
                    "SELECT * FROM seen WHERE torrent_url=?", (SOURCE,)
                ).fetchone()
            )
            == seen_before
        )
        if expire_seen:
            # Model normal receipt expiry, keeping both current conflicting claims.
            db.execute("DELETE FROM seen")
    _poll_removed(pack, monkeypatch)
    _poll_removed(pack, monkeypatch)
    assert _snapshot() == before
    with shared.get_db() as db:
        seen_after = db.execute(
            "SELECT * FROM seen WHERE torrent_url=?", (SOURCE,)
        ).fetchone()
        if expire_seen:
            assert seen_after is None
        else:
            assert seen_after is not None
            assert dict(seen_after) == seen_before
        assert (
            db.execute(
                "SELECT 1 FROM history WHERE event_type='grab_failed'"
            ).fetchone()
            is None
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM events WHERE message='Orphan cleanup deferred: conflicting claim sources'"
            ).fetchone()[0]
            == 1
        )
    if not expire_seen:
        _assert_guid_dedup(pack)


@pytest.mark.parametrize("change", ["claim", "seen", "fork", "queue"])
def test_cleanup_recaptures_evidence_after_enumeration_closes(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    import import_discovery
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute(
            "DELETE FROM chapters WHERE id != ?", (30 if change == "fork" else -1,)
        )
    expected = _snapshot()
    if change == "claim":
        row = next(row for row in expected["volumes"] if row["id"] == 2)
        row.update(source_url="https://successor.invalid", download_id="successor-id")
    elif change == "fork":
        expected["chapters"][0]["torrent_url"] = "https://successor.invalid"
    real_get_db = import_discovery.get_db
    entries = 0
    injected = False

    @contextmanager
    def publish_after_enumeration() -> Generator[sqlite3.Connection, None, None]:
        nonlocal entries, injected
        entries += 1
        entry = entries
        with real_get_db() as db:
            yield db
        if entry == 1:
            # Commit a competitor exactly after enumeration's connection closes.
            with shared.get_db() as db:
                if change == "claim":
                    db.execute(
                        "UPDATE volumes SET source_url='https://successor.invalid',download_id='successor-id' WHERE id=2"
                    )
                elif change == "seen":
                    db.execute("UPDATE seen SET download_id='successor-id'")
                elif change == "fork":
                    db.execute(
                        "UPDATE chapters SET torrent_url='https://successor.invalid' WHERE id=30"
                    )
                else:
                    db.execute(
                        "INSERT INTO import_queue(series_id,download_id,download_client_id,status) VALUES(1,?,7,'pending')",
                        (pack.download_id,),
                    )
            injected = True

    monkeypatch.setattr(import_discovery, "get_db", publish_after_enumeration)
    import_discovery._cleanup_missing_downloads(
        set(),
        download_client_id=7,
        protocol=pack.protocol,
        client_type=pack.client_type,
        include_legacy_ownerless=False,
    )
    assert injected
    assert _snapshot() == expected
    with shared.get_db() as db:
        assert (
            db.execute(
                "SELECT 1 FROM history WHERE event_type='grab_failed'"
            ).fetchone()
            is None
        )
        assert db.execute("SELECT 1 FROM seen").fetchone() is not None
        if change == "queue":
            assert tuple(
                db.execute(
                    "SELECT status,torrent_url,download_protocol FROM import_queue"
                ).fetchone()
            ) == ("pending", None, None)


@pytest.mark.parametrize("missing", ["source", "protocol", "both"])
def test_unknown_terminal_queue_identity_cannot_grant_failed_retry(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    import shared

    queue_id = _queue(pack)
    source = None if missing in ("source", "both") else SOURCE
    protocol = None if missing in ("protocol", "both") else pack.protocol
    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("DELETE FROM chapters")
        db.execute(
            "UPDATE import_queue SET status='failed',torrent_url=?,download_protocol=? WHERE id=?",
            (source, protocol, queue_id),
        )
        db.execute(
            "INSERT INTO import_queue(series_id,download_id,download_client_id,download_protocol,torrent_url,status) VALUES(1,?,7,?,?,'imported')",
            (pack.download_id, protocol, source),
        )
        before = {
            table: [
                tuple(row)
                for row in db.execute(
                    f"SELECT * FROM {table} ORDER BY {'torrent_url' if table == 'seen' else 'id'}"
                )
            ]
            for table in ("import_queue", "import_queue_files", "seen")
        }
    claims_before = _snapshot()
    _poll_removed(pack, monkeypatch)
    _poll_removed(pack, monkeypatch)
    assert _snapshot() == claims_before
    with shared.get_db() as db:
        assert {
            table: [
                tuple(row)
                for row in db.execute(
                    f"SELECT * FROM {table} ORDER BY {'torrent_url' if table == 'seen' else 'id'}"
                )
            ]
            for table in before
        } == before
        assert (
            db.execute(
                "SELECT 1 FROM history WHERE event_type='grab_failed'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("evidence_table", ["volumes", "chapters"])
@pytest.mark.parametrize(
    "client_label", [None, "", "conflicting"], ids=["null", "blank", "conflicting"]
)
@pytest.mark.parametrize("path_bearing", [False, True])
def test_current_fork_evidence_is_independent_of_client_label_and_path(
    pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    evidence_table: str,
    client_label: str | None,
    path_bearing: bool,
) -> None:
    import shared

    label = (
        ("sabnzbd" if pack.client_type == "qbittorrent" else "qbittorrent")
        if client_label == "conflicting"
        else client_label
    )
    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("DELETE FROM chapters WHERE id != 30")
        db.execute("UPDATE chapters SET torrent_url='https://successor.invalid'")
        row_id = 2 if evidence_table == "volumes" else 30
        db.execute(
            f"UPDATE {evidence_table} SET client=?,import_path=? WHERE id=?",
            (label, str(pack.original) if path_bearing else None, row_id),
        )
        # Receipt expiry does not erase the two surviving exact-identity claims.
        db.execute("DELETE FROM seen")
        volume = db.execute(
            "SELECT series_id,download_client_id,protocol,download_id,source_url FROM volumes WHERE id=2"
        ).fetchone()
        chapter = db.execute(
            "SELECT series_id,download_client_id,protocol,download_id,torrent_url FROM chapters WHERE id=30"
        ).fetchone()
        assert tuple(volume)[:4] == tuple(chapter)[:4]
        assert volume[4] != chapter[4]
        rows_before = {
            table: [
                tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")
            ]
            for table in ("volumes", "chapters", "import_queue", "import_queue_files")
        }
    for _ in range(3):
        _poll_removed(pack, monkeypatch)
        with shared.get_db() as db:
            assert {
                table: [
                    tuple(row)
                    for row in db.execute(f"SELECT * FROM {table} ORDER BY id")
                ]
                for table in rows_before
            } == rows_before
            assert db.execute("SELECT 1 FROM seen").fetchone() is None
            assert (
                db.execute(
                    "SELECT 1 FROM history WHERE event_type='grab_failed'"
                ).fetchone()
                is None
            )
    with shared.get_db() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM events WHERE message='Orphan cleanup deferred: conflicting claim sources'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("protected_table", ["volumes", "chapters"])
def test_conflicting_client_label_does_not_gain_mutation_authority(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, protected_table: str
) -> None:
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("DELETE FROM chapters WHERE id != 30")
        row_id = 2 if protected_table == "volumes" else 30
        db.execute(
            f"UPDATE {protected_table} SET client=? WHERE id=?",
            ("sabnzbd" if pack.client_type == "qbittorrent" else "qbittorrent", row_id),
        )
        protected_before = tuple(
            db.execute(
                f"SELECT * FROM {protected_table} WHERE id=?", (row_id,)
            ).fetchone()
        )
    _poll_removed(pack, monkeypatch)
    _poll_removed(pack, monkeypatch)
    with shared.get_db() as db:
        assert (
            tuple(
                db.execute(
                    f"SELECT * FROM {protected_table} WHERE id=?", (row_id,)
                ).fetchone()
            )
            == protected_before
        )
        eligible_table = "chapters" if protected_table == "volumes" else "volumes"
        eligible_id = 30 if protected_table == "volumes" else 2
        assert tuple(
            db.execute(
                f"SELECT status,download_id FROM {eligible_table} WHERE id=?",
                (eligible_id,),
            ).fetchone()
        ) == ("wanted", None)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='grab_failed'"
            ).fetchone()[0]
            == 1
        )


def test_independent_download_identities_both_remain_retryable(
    pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("DELETE FROM chapters WHERE id != 30")
        db.execute(
            "UPDATE chapters SET torrent_url='https://independent.invalid',download_id='independent-id'"
        )
        db.execute(
            "INSERT INTO seen(torrent_url,torrent_name,release_guid,series_id,download_client_id,protocol,client,download_id,respect_grab_claims) VALUES('https://independent.invalid','Independent','independent-guid',1,7,?,?,?,1)",
            (pack.protocol, pack.client_type, "independent-id"),
        )
    _poll_removed(pack, monkeypatch)
    after = _snapshot()
    _poll_removed(pack, monkeypatch)
    _poll_removed(pack, monkeypatch)
    assert _snapshot() == after
    with shared.get_db() as db:
        assert (
            db.execute("SELECT status FROM volumes WHERE id=2").fetchone()[0]
            == "wanted"
        )
        assert (
            db.execute("SELECT status FROM chapters WHERE id=30").fetchone()[0]
            == "wanted"
        )
        assert db.execute("SELECT 1 FROM seen").fetchone() is None
        assert {
            row[0]
            for row in db.execute(
                "SELECT torrent_url FROM history WHERE event_type='grab_failed'"
            )
        } == {SOURCE, "https://independent.invalid"}
        assert (
            db.execute(
                "SELECT COUNT(*) FROM history WHERE event_type='grab_failed'"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize(
    "nominal", ["imported_history", "skipped_history", "imported_queue"]
)
@pytest.mark.parametrize("failed", [False, True])
def test_nominal_refusal_and_exact_failed_retry_precedence(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, nominal: str, failed: bool
) -> None:
    from events import add_history
    import shared

    queue_id = _queue(pack)
    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2,3)")
        db.execute("DELETE FROM chapters WHERE id NOT IN (20,24,30)")
        if failed:
            db.execute(
                "UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,)
            )
        else:
            db.execute("DELETE FROM import_queue_files WHERE queue_id=?", (queue_id,))
            db.execute("DELETE FROM import_queue WHERE id=?", (queue_id,))
        if nominal == "imported_queue":
            cursor = db.execute(
                "INSERT INTO import_queue(series_id,download_id,download_client_id,download_protocol,torrent_url,status) VALUES(1,?,7,?,?,'imported')",
                (pack.download_id, pack.protocol, SOURCE),
            )
            db.execute(
                "INSERT INTO import_queue_files(queue_id,filename,status) VALUES(?,'legacy.cbz','skipped')",
                (cursor.lastrowid,),
            )
        else:
            add_history(
                db,
                "imported" if nominal == "imported_history" else "import_skipped",
                1,
                "Owned Pack",
                "",
                protocol=pack.protocol,
                torrent_url=SOURCE,
                download_id=pack.download_id,
                download_client_id=7,
                data={"count": 1}
                if nominal == "imported_history"
                else {"count": 0, "skipped_count": 1, "reason": "all_files_skipped"},
            )
        seen_before = [dict(row) for row in db.execute("SELECT * FROM seen")]
        queues_before = [
            dict(row) for row in db.execute("SELECT * FROM import_queue ORDER BY id")
        ]
        files_before = [
            dict(row)
            for row in db.execute("SELECT * FROM import_queue_files ORDER BY id")
        ]
    before = _snapshot()
    _poll_removed(pack, monkeypatch)
    _poll_removed(pack, monkeypatch)
    with shared.get_db() as db:
        if failed:
            assert [
                tuple(row)
                for row in db.execute(
                    "SELECT status,download_id FROM volumes WHERE id IN (2,3)"
                )
            ] == [("wanted", None)] * 2
            assert [
                tuple(row)
                for row in db.execute(
                    "SELECT status,download_id FROM chapters ORDER BY id"
                )
            ] == [("wanted", None)] * 3
            assert db.execute("SELECT 1 FROM seen").fetchone() is None
            assert (
                db.execute(
                    "SELECT status FROM import_queue WHERE id=?", (queue_id,)
                ).fetchone()[0]
                == "skipped"
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM history WHERE event_type='grab_failed'"
                ).fetchone()[0]
                == 1
            )
        else:
            assert _snapshot() == before
            assert [
                dict(row) for row in db.execute("SELECT * FROM seen")
            ] == seen_before
            assert [
                dict(row)
                for row in db.execute("SELECT * FROM import_queue ORDER BY id")
            ] == queues_before
            assert [
                dict(row)
                for row in db.execute("SELECT * FROM import_queue_files ORDER BY id")
            ] == files_before
            assert (
                db.execute(
                    "SELECT 1 FROM history WHERE event_type='grab_failed'"
                ).fetchone()
                is None
            )
            diagnostics = db.execute(
                "SELECT message FROM events WHERE message LIKE 'Orphan cleanup deferred:%'"
            ).fetchall()
            assert len(diagnostics) == 1
            assert "ambiguous" in diagnostics[0][0]


@pytest.mark.parametrize(
    "difference", ["source", "owner", "protocol", "series", "case"]
)
def test_foreign_failed_outcome_cannot_override_nominal_refusal(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, difference: str
) -> None:
    from events import add_history
    import shared

    queue_id = _queue(pack)
    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2,3)")
        db.execute("DELETE FROM chapters")
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
        if difference == "source":
            db.execute("UPDATE import_queue SET torrent_url='https://other.invalid'")
        elif difference == "owner":
            db.execute("UPDATE import_queue SET download_client_id=8")
        elif difference == "protocol":
            db.execute(
                "UPDATE import_queue SET download_protocol=?",
                ("nzb" if pack.protocol == "torrent" else "torrent",),
            )
        elif difference == "series":
            db.execute("UPDATE import_queue SET series_id=2")
        else:
            db.execute(
                "UPDATE import_queue SET download_id=?", (pack.download_id.swapcase(),)
            )
        add_history(
            db,
            "import_skipped",
            1,
            "Owned Pack",
            "",
            protocol=pack.protocol,
            torrent_url=SOURCE,
            download_id=pack.download_id,
            download_client_id=7,
            data={"count": 0, "reason": "all_files_skipped"},
        )
    before = _snapshot()
    _poll_removed(pack, monkeypatch)
    _poll_removed(pack, monkeypatch)
    with shared.get_db() as db:
        if difference == "case" and pack.protocol == "torrent":
            assert (
                db.execute("SELECT status FROM volumes WHERE id=2").fetchone()[0]
                == "wanted"
            )
        else:
            assert _snapshot() == before
            assert (
                db.execute(
                    "SELECT 1 FROM seen WHERE torrent_url=?", (SOURCE,)
                ).fetchone()
                is not None
            )
            assert (
                db.execute(
                    "SELECT status FROM import_queue WHERE id=?", (queue_id,)
                ).fetchone()[0]
                == "failed"
            )
            assert (
                db.execute(
                    "SELECT 1 FROM history WHERE event_type='grab_failed'"
                ).fetchone()
                is None
            )


@pytest.mark.parametrize("outer", ["deferred_reader", "writer"])
def test_reservation_upgrades_existing_transaction_before_fences(
    pack: Pack, outer: str
) -> None:
    import import_discovery
    import shared

    with shared.get_db() as db:
        db.execute("BEGIN" if outer == "deferred_reader" else "BEGIN IMMEDIATE")
        db.execute("SELECT id FROM import_queue").fetchall()
        assert import_discovery._reserve_orphan_cleanup(
            db, pack.download_id, download_client_id=7, protocol=pack.protocol
        )[0]
        with closing(sqlite3.connect(shared.DB_PATH, timeout=0)) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")


@pytest.mark.parametrize("kind", ["local_volume", "local_stub", "protected_stub"])
def test_protected_observations_do_not_create_failed_grab_poll_loop(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("DELETE FROM chapters WHERE id != 21")
        if kind != "protected_stub":
            db.execute(
                "UPDATE volumes SET import_path=? WHERE id=2", (str(pack.original),)
            )
        if kind != "local_volume":
            db.execute(
                "UPDATE volumes SET volume_num=NULL,pack_type='chapter' WHERE id=2"
            )
        seen = [dict(row) for row in db.execute("SELECT * FROM seen")]
    before = _snapshot()
    _poll_removed(pack, monkeypatch)
    _poll_removed(pack, monkeypatch)
    assert _snapshot() == before
    with shared.get_db() as db:
        assert [dict(row) for row in db.execute("SELECT * FROM seen")] == seen
        assert (
            db.execute(
                "SELECT 1 FROM history WHERE event_type='grab_failed'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("difference", ["source", "owner", "protocol", "id"])
def test_conflicting_current_seen_does_not_authorize_claim_reset(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, difference: str
) -> None:
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2,3)")
        db.execute("DELETE FROM chapters")
        if difference == "source":
            db.execute("UPDATE seen SET torrent_url='https://new.invalid'")
        elif difference == "owner":
            db.execute("UPDATE seen SET download_client_id=8")
        elif difference == "protocol":
            db.execute(
                "UPDATE seen SET protocol=?",
                ("nzb" if pack.protocol == "torrent" else "torrent",),
            )
        else:
            db.execute("UPDATE seen SET download_id='successor'")
        seen = [dict(row) for row in db.execute("SELECT * FROM seen")]
    before = _snapshot()
    _poll_removed(pack, monkeypatch)
    _poll_removed(pack, monkeypatch)
    assert _snapshot() == before
    with shared.get_db() as db:
        assert [dict(row) for row in db.execute("SELECT * FROM seen")] == seen
        assert (
            db.execute(
                "SELECT 1 FROM history WHERE event_type='grab_failed'"
            ).fetchone()
            is None
        )


def test_preparation_failure_and_success_share_nominal_terminal_receipts(
    pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import import_commit
    import import_execute
    import import_queue
    import shared

    transitions: list[tuple[str, tuple[str, ...]]] = []
    real_transition = import_commit.transition_import_queue_row

    def observe_transition(
        db: sqlite3.Connection,
        queue_id: int,
        lease_owner: str,
        new_status: ImportQueueStatus,
    ) -> bool:
        result = real_transition(db, queue_id, lease_owner, new_status)
        if result:
            transitions.append(
                (
                    str(
                        db.execute(
                            "SELECT status FROM import_queue WHERE id=?", (queue_id,)
                        ).fetchone()[0]
                    ),
                    tuple(
                        str(row[0])
                        for row in db.execute(
                            "SELECT status FROM import_queue_files WHERE queue_id=? ORDER BY id",
                            (queue_id,),
                        )
                    ),
                )
            )
        return result

    monkeypatch.setattr(
        import_commit, "transition_import_queue_row", observe_transition
    )
    before = _snapshot()
    original_bytes = pack.original.read_bytes()
    candidate_bytes = pack.candidate.read_bytes()
    _commit_skip(pack)
    with shared.get_db() as db:
        success = dict(
            db.execute(
                "SELECT * FROM history WHERE event_type='import_skipped'"
            ).fetchone()
        )
        db.execute("DELETE FROM history")
        # Restore the original acquisition to compare the two real paths.
        for table in ("volumes", "chapters"):
            for row in before[table]:
                columns = [name for name in row if name != "id"]
                db.execute(
                    f"UPDATE {table} SET "
                    + ",".join(f"{name}=?" for name in columns)
                    + " WHERE id=?",
                    (*[row[name] for name in columns], row["id"]),
                )
    queue_id = _queue(pack)
    failed_before = _snapshot()

    def deny_directory(path: str) -> None:
        assert path == str(pack.original.parent)
        raise PermissionError("fixture destination preparation denied")

    real_client = httpx.AsyncClient

    def reject_http(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("preparation failure must not reach a client")

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(reject_http), **kwargs)

    monkeypatch.setattr(import_execute, "ensure_durable_directory", deny_directory)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    assert asyncio.run(import_execute._execute_import(queue_id)) is False
    with shared.get_db() as db:
        failure = dict(
            db.execute(
                "SELECT * FROM history WHERE event_type='import_skipped'"
            ).fetchone()
        )
        assert {
            key: value
            for key, value in success.items()
            if key not in ("id", "created_at")
        } == {
            key: value
            for key, value in failure.items()
            if key not in ("id", "created_at")
        }
        assert json.loads(failure["data"]) == {
            "count": 0,
            "skipped_count": 1,
            "reason": "all_files_skipped",
        }
        assert db.execute("SELECT 1 FROM import_queue").fetchone() is None
        assert db.execute("SELECT 1 FROM import_queue_files").fetchone() is None
        assert db.execute("SELECT 1 FROM import_publications").fetchone() is None
        assert import_queue._has_terminal_download_receipt(
            db,
            series_id=1,
            torrent_url=SOURCE,
            identity=DownloadIdentity(7, pack.protocol, pack.download_id),
        )
    assert transitions == [("imported", ("skipped",)), ("imported", ("skipped",))]
    assert _snapshot() == failed_before
    assert pack.original.read_bytes() == original_bytes
    assert pack.candidate.read_bytes() == candidate_bytes


@pytest.mark.parametrize(
    "row_kind",
    [
        "downloaded_child",
        "other_owner_child",
        "local_claim",
        "mapped_wanted",
        "other_source_child",
        "other_protocol_child",
        "local_child",
    ],
)
def test_missing_client_conserves_nonclaim_observations(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, row_kind: str
) -> None:
    import shared

    child_ids = {"downloaded_child": 21, "other_owner_child": 22, "mapped_wanted": 23}
    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        if row_kind == "local_claim":
            db.execute("DELETE FROM chapters")
            db.execute(
                "UPDATE volumes SET import_path=? WHERE id=2", (str(pack.original),)
            )
        else:
            db.execute(
                "DELETE FROM chapters WHERE id != ?", (child_ids.get(row_kind, 20),)
            )
            if row_kind == "other_source_child":
                db.execute("UPDATE chapters SET torrent_url='https://other.invalid'")
            elif row_kind == "other_protocol_child":
                db.execute(
                    "UPDATE chapters SET protocol=?",
                    ("nzb" if pack.protocol == "torrent" else "torrent",),
                )
            elif row_kind == "local_child":
                db.execute("UPDATE chapters SET import_path=?", (str(pack.original),))
    before = _snapshot()
    _poll_removed(pack, monkeypatch)
    after = _snapshot()
    if row_kind == "local_claim":
        assert after["volumes"] == before["volumes"]
    else:
        assert after["chapters"] == before["chapters"]


def test_missing_client_never_mutates_archive_bytes(
    pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_bytes = pack.original.read_bytes()
    candidate_bytes = pack.candidate.read_bytes()
    _poll_removed(pack, monkeypatch)
    assert pack.original.read_bytes() == original_bytes
    assert pack.candidate.read_bytes() == candidate_bytes


@pytest.mark.parametrize("retain_seen", [False, True])
def test_chapter_only_owner_is_discovered(pack: Pack, retain_seen: bool) -> None:
    import import_discovery
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM volumes")
        db.execute("DELETE FROM chapters WHERE id != 30")
        if not retain_seen:
            db.execute("DELETE FROM seen")
        db.execute(
            "INSERT INTO download_clients(id,name,type,host,enabled) VALUES(7,'fixture',?,'http://downloader.invalid',1)",
            (pack.client_type,),
        )
    partitions = import_discovery._download_client_poll_partitions(pack.client_type)
    assert [(row.client_id, row.client_type) for row in partitions] == [
        (7, pack.client_type)
    ]


def test_chapter_only_missing_client_releases_genuine_unhandled_claim(
    pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM volumes")
        db.execute("DELETE FROM chapters WHERE id != 30")
    _poll_removed(pack, monkeypatch)
    with shared.get_db() as db:
        assert tuple(
            db.execute("SELECT status,download_id FROM chapters WHERE id=30").fetchone()
        ) == ("wanted", None)


@pytest.mark.parametrize("local_stub", [False, True])
def test_null_volume_stub_preserves_child_and_local_observations(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, local_stub: bool
) -> None:
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("DELETE FROM chapters WHERE id != 21")
        db.execute(
            "UPDATE volumes SET volume_num=NULL,chapter_num=1,pack_type='chapter',import_path=? WHERE id=2",
            (str(pack.original.parent) if local_stub else None,),
        )
    before = _snapshot()
    _poll_removed(pack, monkeypatch)
    after = _snapshot()
    assert after["chapters"] == before["chapters"], (
        "stub removal must not erase independent child linkage"
    )
    if local_stub:
        assert after["volumes"] == before["volumes"]


def test_unhandled_empty_pathless_stub_remains_cleanup_candidate(
    pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shared

    with shared.get_db() as db:
        db.execute("DELETE FROM chapters")
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute(
            "UPDATE volumes SET volume_num=NULL,chapter_num=1,pack_type='chapter' WHERE id=2"
        )
    _poll_removed(pack, monkeypatch)
    with shared.get_db() as db:
        assert db.execute("SELECT 1 FROM volumes WHERE id=2").fetchone() is None


@pytest.mark.parametrize(
    "difference", ["source", "owner", "protocol", "series", "case"]
)
def test_foreign_failed_queue_and_children_are_not_transitioned(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, difference: str
) -> None:
    import shared

    queue_id = _queue(pack)
    with shared.get_db() as db:
        db.execute("DELETE FROM chapters")
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
        if difference == "source":
            db.execute("UPDATE import_queue SET torrent_url='https://other.invalid'")
        elif difference == "owner":
            db.execute("UPDATE import_queue SET download_client_id=8")
        elif difference == "protocol":
            db.execute(
                "UPDATE import_queue SET download_protocol=?",
                ("nzb" if pack.protocol == "torrent" else "torrent",),
            )
        elif difference == "series":
            db.execute("UPDATE import_queue SET series_id=2")
        else:
            db.execute(
                "UPDATE import_queue SET download_id=?", (pack.download_id.swapcase(),)
            )
        children_before = [
            tuple(row)
            for row in db.execute("SELECT * FROM import_queue_files ORDER BY id")
        ]
    _poll_removed(pack, monkeypatch)
    with shared.get_db() as db:
        status = db.execute(
            "SELECT status FROM import_queue WHERE id=?", (queue_id,)
        ).fetchone()[0]
        if difference == "case" and pack.protocol == "torrent":
            assert status == "skipped"
        else:
            assert status == "failed", (
                "foreign acquisition was treated as the cleanup target"
            )
            assert [
                tuple(row)
                for row in db.execute("SELECT * FROM import_queue_files ORDER BY id")
            ] == children_before


def test_partial_history_boolean_is_not_terminal_authority(pack: Pack) -> None:
    from events import add_history
    import import_queue
    import shared

    queue_id = _queue(pack)
    with shared.get_db() as db:
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
        add_history(
            db,
            "imported",
            1,
            "Owned Pack",
            "",
            protocol=pack.protocol,
            torrent_url=SOURCE,
            download_id=pack.download_id,
            download_client_id=7,
            data={"count": 1},
        )
        assert import_queue._has_terminal_download_receipt(
            db,
            series_id=1,
            torrent_url=SOURCE,
            identity=DownloadIdentity(7, pack.protocol, pack.download_id),
        )
        assert (
            db.execute(
                "SELECT status FROM import_queue WHERE id=?", (queue_id,)
            ).fetchone()[0]
            == "failed"
        )


def test_purged_history_without_surviving_proof_is_not_terminal_evidence(
    pack: Pack,
) -> None:
    import import_queue
    import shared

    _commit_skip(pack)
    with shared.get_db() as db:
        db.execute("DELETE FROM history")
        assert db.execute("SELECT 1 FROM import_queue").fetchone() is None
        assert not import_queue._has_terminal_download_receipt(
            db,
            series_id=1,
            torrent_url=SOURCE,
            identity=DownloadIdentity(7, pack.protocol, pack.download_id),
        )


@pytest.mark.parametrize("queue_state", ["absent", "imported", "failed"])
def test_reservation_owns_writer_before_return(pack: Pack, queue_state: str) -> None:
    import import_discovery
    import shared

    if queue_state != "absent":
        queue_id = _queue(pack)
        with shared.get_db() as db:
            db.execute(
                "UPDATE import_queue SET status=? WHERE id=?", (queue_state, queue_id)
            )
    with shared.get_db() as db:
        assert import_discovery._reserve_orphan_cleanup(
            db, pack.download_id, download_client_id=7, protocol=pack.protocol
        )[0]
        with closing(sqlite3.connect(shared.DB_PATH, timeout=0)) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")


@pytest.mark.parametrize("fence", ["pending", "lease", "publication"])
def test_fence_committed_after_reservation_cannot_be_overwritten(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, fence: str
) -> None:
    import import_discovery
    import shared

    real_reserve = import_discovery._reserve_orphan_cleanup
    inserted: list[int] = []
    with shared.get_db() as db:
        db.execute("DELETE FROM chapters")
        db.execute("DELETE FROM volumes WHERE id NOT IN (1,2)")
    before = _snapshot()

    def reserve_then_publish_fence(
        db: sqlite3.Connection,
        download_id: str,
        *,
        download_client_id: int | None,
        protocol: DownloadProtocol,
        **scope: Any,
    ) -> tuple[bool, list[int]]:
        result = real_reserve(
            db,
            download_id,
            download_client_id=download_client_id,
            protocol=protocol,
            **scope,
        )
        try:
            with closing(sqlite3.connect(shared.DB_PATH, timeout=0)) as writer, writer:
                writer.execute("BEGIN IMMEDIATE")
                cursor = writer.execute(
                    "INSERT INTO import_queue(series_id,download_id,download_client_id,download_protocol,torrent_url,status,lease_owner) VALUES(1,?,7,?,?,?,?)",
                    (
                        download_id,
                        protocol,
                        SOURCE,
                        "pending" if fence == "pending" else "failed",
                        "successor" if fence == "lease" else None,
                    ),
                )
                queue_id = cursor.lastrowid
                assert queue_id is not None
                if fence == "publication":
                    writer.execute(
                        "INSERT INTO import_publications(queue_id,state,owner_token,series_id,dst_dir,import_mode,staging_dir,queue_snapshot_json,series_tags_json,queue_status) VALUES(?,'published','successor',1,?,'copy',?,'{}','[]','importing')",
                        (
                            queue_id,
                            str(pack.original.parent),
                            str(pack.root / "staging"),
                        ),
                    )
                inserted.append(queue_id)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
        return result

    monkeypatch.setattr(
        import_discovery, "_reserve_orphan_cleanup", reserve_then_publish_fence
    )
    _poll_removed(pack, monkeypatch)
    if inserted:
        assert _snapshot() == before, (
            "a committed successor fence was bypassed by stale cleanup authorization"
        )
        with shared.get_db() as db:
            assert (
                db.execute(
                    "SELECT 1 FROM seen WHERE torrent_url=?", (SOURCE,)
                ).fetchone()
                is not None
            )
