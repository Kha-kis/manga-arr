"""Explicit payload bounds take precedence over complete-pack classification."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
import json
from pathlib import Path
from sqlite3 import Row
from typing import cast
from unittest.mock import AsyncMock

import pytest

import grab_backlog
import grab_core
import grab_dedup
import main
import metadata_enrichment
import schema
import shared
from clients import GrabResult


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncMock]:
    monkeypatch.setattr(shared, "DB_PATH", str(tmp_path / "coverage.db"))
    monkeypatch.setattr(shared, "CONFIG", {})
    schema.init_db()
    with shared.get_db() as db:
        _ = db.execute(
            "INSERT INTO series(id,title,search_pattern,total_volumes,total_chapters,status)"
            + " VALUES(1,'Test Series','Test Series',10,100,'RELEASING')"
        )
        for number in range(1, 11):
            _ = db.execute(
                "INSERT INTO volumes(id,series_id,volume_num,status,monitored,quality)"
                + " VALUES(?,1,?,?,1,'cbz')",
                (number, number, "downloaded" if number < 10 else "wanted"),
            )
            _ = db.execute(
                "INSERT INTO chapters(series_id,volume_id,chapter_num,status,monitored)"
                + " VALUES(1,?,?,'wanted',1)",
                (number, number * 10),
            )
    client = AsyncMock(return_value=GrabResult(True, "qbittorrent", "hash", True, None))
    monkeypatch.setattr(grab_core, "grab_url", client)
    monkeypatch.setattr(grab_core, "notify_discord", AsyncMock())
    grab_dedup._GRABBING_URLS.clear()  # pyright: ignore[reportPrivateUsage]
    yield client
    assert not grab_dedup._GRABBING_URLS  # pyright: ignore[reportPrivateUsage]


def release(label: str) -> dict[str, str]:
    return {
        "title": f"Test Series {label}",
        "url": f"https://indexer.invalid/{label.replace(' ', '-')}",
        "protocol": "torrent",
    }


def observations() -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with shared.get_db() as db:
        volumes = cast(
            list[Row], db.execute("SELECT * FROM volumes ORDER BY id").fetchall()
        )
        chapters = cast(
            list[Row], db.execute("SELECT * FROM chapters ORDER BY id").fetchall()
        )
        return (
            [tuple(cast(Iterable[object], r)) for r in volumes],
            [tuple(cast(Iterable[object], r)) for r in chapters],
        )


def statuses(table: str, column: str) -> dict[float, str]:
    assert (table, column) in {("volumes", "volume_num"), ("chapters", "chapter_num")}
    with shared.get_db() as db:
        rows = cast(
            list[Row], db.execute(f"SELECT {column},status FROM {table}").fetchall()
        )
        return {
            float(cast(float, r[0])): cast(str, r[1]) for r in rows if r[0] is not None
        }


@pytest.mark.parametrize("label", ["v01-v09", "v01-v08 Complete Series"])
def test_backlog_rejects_complete_range_missing_only_wanted_volume(
    env: AsyncMock, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    before = observations()
    monkeypatch.setattr(
        grab_backlog, "_search_all", AsyncMock(return_value=[release(label)])
    )
    assert asyncio.run(grab_backlog.grab_existing(1, "Test Series", "Test Series")) == 0
    env.assert_not_awaited()
    assert observations() == before
    with shared.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0


@pytest.mark.parametrize("label", ["v01-v09", "v01-v08 Complete Series"])
def test_complete_coverage_cannot_borrow_outside_gap(label: str) -> None:
    from parsing import extract_volume_range

    assert metadata_enrichment._coverage_already_grabbed(  # pyright: ignore[reportPrivateUsage]
        1, "complete", extract_volume_range(release(label)["title"]), None, {}, 100, 10
    )


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("label", ["v01-v09", "v01-v08 Complete Series"])
def test_complete_claims_only_represented_work(manual: bool, label: str) -> None:
    with shared.get_db() as db:
        _ = db.execute("UPDATE volumes SET status='wanted' WHERE id=2")
        _ = db.execute("UPDATE volumes SET import_path='/fixture/local.cbz' WHERE id=9")
        if manual:
            _ = db.execute("UPDATE series SET monitored=0")
            _ = db.execute("UPDATE volumes SET monitored=0")
    before_vols, before_chapters = observations()
    assert asyncio.run(
        grab_core.grab_item(release(label), 1, respect_monitoring=not manual)
    )
    after_vols, after_chapters = observations()
    assert after_vols[1] != before_vols[1]
    assert statuses("volumes", "volume_num")[2] == "grabbed"
    assert [r for i, r in enumerate(after_vols[:10]) if i != 1] == [
        r for i, r in enumerate(before_vols) if i != 1
    ]
    assert statuses("chapters", "chapter_num")[20] == "grabbed"
    assert [r for i, r in enumerate(after_chapters) if i != 1] == [
        r for i, r in enumerate(before_chapters) if i != 1
    ]
    with shared.get_db() as db:
        pack = cast(
            Row,
            db.execute(
                "SELECT pack_type,vol_range_start,vol_range_end FROM volumes WHERE volume_num IS NULL"
            ).fetchone(),
        )
        assert tuple(cast(Iterable[object], pack)) == (
            "complete",
            1,
            9 if label == "v01-v09" else 8,
        )


@pytest.mark.parametrize("label", ["v01-v10", "Complete Series"])
def test_full_range_and_unbounded_complete_controls(label: str) -> None:
    assert asyncio.run(grab_core.grab_item(release(label), 1))
    assert statuses("volumes", "volume_num")[10] == "grabbed"
    assert statuses("chapters", "chapter_num")[100] == "grabbed"


def test_existing_bounded_complete_does_not_cover_later_volume() -> None:
    with shared.get_db() as db:
        _ = db.execute("UPDATE volumes SET status='wanted' WHERE id=2")
    assert asyncio.run(grab_core.grab_item(release("v01-v09"), 1))
    assert not metadata_enrichment._coverage_already_grabbed(  # pyright: ignore[reportPrivateUsage]
        1, "volume", (9, 10), None, {}, 100, 10
    )
    assert asyncio.run(grab_core.grab_item(release("v09-v10"), 1))
    assert statuses("volumes", "volume_num")[10] == "grabbed"


def test_fractional_wanted_inside_complete_range_is_not_truncated() -> None:
    with shared.get_db() as db:
        _ = db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,monitored) VALUES(1,3.5,'wanted',1)"
        )
        _ = db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,monitored,is_special) VALUES(1,3.01,'wanted',1,1)"
        )
    assert asyncio.run(grab_core.grab_item(release("v01-v09"), 1))
    state = statuses("volumes", "volume_num")
    assert state[3.5] == "grabbed"
    assert state[3.01] == "wanted"
    assert state[3] == "downloaded"
    assert state[10] == "wanted"


def test_fractional_explicit_endpoints_bound_complete_claims() -> None:
    with shared.get_db() as db:
        for number in (3.01, 3.25, 3.5):
            _ = db.execute(
                "INSERT INTO volumes(series_id,volume_num,status,monitored)"
                + " VALUES(1,?,'wanted',1)",
                (number,),
            )
    assert asyncio.run(grab_core.grab_item(release("v03.01-v03.5 Complete Series"), 1))
    state = statuses("volumes", "volume_num")
    assert {number for number, status in state.items() if status == "grabbed"} == {
        3.01,
        3.25,
        3.5,
    }
    assert state[3] == "downloaded"
    assert state[10] == "wanted"


@pytest.mark.parametrize("quality,accepted", [("cbr", True), ("cbz", False)])
def test_in_range_single_volume_quality_policy_is_unchanged(
    env: AsyncMock, quality: str, accepted: bool
) -> None:
    with shared.get_db() as db:
        _ = db.execute("UPDATE volumes SET quality=? WHERE id=2", (quality,))
    assert asyncio.run(grab_core.grab_item(release("Vol 2.cbz"), 1)) is accepted
    assert statuses("volumes", "volume_num")[2] == (
        "grabbed" if accepted else "downloaded"
    )
    assert statuses("volumes", "volume_num")[10] == "wanted"
    if not accepted:
        env.assert_not_awaited()


def test_mapped_chapter_pack_keeps_existing_volume_semantics() -> None:
    with shared.get_db() as db:
        _ = db.execute("UPDATE volumes SET status='wanted' WHERE id=3")
        _ = db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,monitored) VALUES(1,3.5,'wanted',1)"
        )
        _ = db.execute(
            "UPDATE series SET chapter_vol_map=?",
            (json.dumps({"10": 3, "20": 3, "30": 3}),),
        )
    assert asyncio.run(grab_core.grab_item(release("Chapters 010-030"), 1))
    assert statuses("volumes", "volume_num")[3] == "grabbed"
    assert statuses("volumes", "volume_num")[3.5] == "wanted"
    assert statuses("volumes", "volume_num")[10] == "wanted"
    assert statuses("chapters", "chapter_num")[100] == "wanted"


def test_post_await_monitoring_and_local_observations_survive(
    env: AsyncMock,
) -> None:
    with shared.get_db() as db:
        _ = db.execute("UPDATE volumes SET status='wanted' WHERE id IN (2,3)")

    async def change_observations(*_args: object, **_kwargs: object) -> GrabResult:
        with shared.get_db() as db:
            _ = db.execute("UPDATE volumes SET monitored=0 WHERE id=2")
            _ = db.execute(
                "UPDATE volumes SET status='downloaded',import_path='/fixture/new.cbz' WHERE id=3"
            )
        return GrabResult(True, "qbittorrent", "hash", True, None)

    env.side_effect = change_observations
    assert asyncio.run(grab_core.grab_item(release("v01-v09"), 1))
    state = statuses("volumes", "volume_num")
    assert state[2] == "wanted"
    assert state[3] == "downloaded"
    assert state[10] == "wanted"
    assert statuses("chapters", "chapter_num")[20] == "wanted"
    # The local downloaded observation invokes the existing SQLite cascade.
    assert statuses("chapters", "chapter_num")[30] == "downloaded"


@pytest.mark.parametrize("later_pack", [False, True])
def test_finished_complete_first_leaves_outside_gap_for_later_candidate(
    monkeypatch: pytest.MonkeyPatch, later_pack: bool
) -> None:
    with shared.get_db() as db:
        _ = db.execute("UPDATE series SET status='FINISHED'")
        _ = db.execute("UPDATE volumes SET status='wanted' WHERE id>=5")
    queries: list[str] = []

    async def search(query: str, **_kwargs: object) -> list[dict[str, str]]:
        queries.append(query)
        if query == "Test Series vol 10":
            return [release("Vol 10")]
        candidates = [release("v01-v09")]
        if later_pack:
            candidates.append(release("v09-v10"))
        return candidates

    monkeypatch.setattr(grab_backlog, "_search_all", search)
    assert asyncio.run(grab_backlog.grab_existing(1, "Test Series", "Test Series")) == 2
    assert statuses("volumes", "volume_num")[10] == "grabbed"
    if later_pack:
        with shared.get_db() as db:
            assert (
                db.execute(
                    "SELECT source_url FROM volumes WHERE volume_num=10"
                ).fetchone()[0]
                == release("v09-v10")["url"]
            )
    else:
        assert "Test Series vol 10" in queries
        with shared.get_db() as db:
            assert (
                db.execute(
                    "SELECT source_url FROM volumes WHERE volume_num=10"
                ).fetchone()[0]
                == release("Vol 10")["url"]
            )


@pytest.mark.parametrize(
    "label,stored,bounds",
    [
        ("v01-v09", (None, None), (1, 9)),
        ("v01-v08 Complete Series", (1, 9), (1, 8)),
        ("Complete Series", (1, 9), (1, 9)),
        ("v03.01-v03.5 Complete Series", (None, None), (3.01, 3.5)),
        ("Complete Series", (3.01, 3.5), (3.01, 3.5)),
    ],
)
def test_startup_retains_complete_bounds_and_only_claims_inside(
    label: str,
    stored: tuple[float | None, float | None],
    bounds: tuple[float, float],
) -> None:
    with shared.get_db() as db:
        _ = db.execute("UPDATE volumes SET status='wanted',monitored=0 WHERE id=2")
        for number, special in ((0.5, 0), (3.25, 0), (3.5, 0), (3.01, 1)):
            _ = db.execute(
                "INSERT INTO volumes(series_id,volume_num,status,monitored,is_special)"
                + " VALUES(1,?,'wanted',1,?)",
                (number, special),
            )
        _ = db.execute(
            "INSERT INTO volumes(series_id,status,torrent_name,pack_type,vol_range_start,vol_range_end)"
            + " VALUES(1,'grabbed',?,'complete',?,?)",
            (release(label)["title"], *stored),
        )
    expected_claims = 3 if bounds[0] == 1 else 2
    assert main.backfill_pack_ranges() == expected_claims
    state = statuses("volumes", "volume_num")
    assert state[2] == ("grabbed" if bounds[0] == 1 else "wanted")
    assert state[3.25] == state[3.5] == "grabbed"
    assert state[3.01] == state[0.5] == state[10] == "wanted"
    assert state[3] == "downloaded"
    with shared.get_db() as db:
        pack = cast(
            Row,
            db.execute(
                "SELECT pack_type,vol_range_start,vol_range_end FROM volumes WHERE volume_num IS NULL"
            ).fetchone(),
        )
        assert tuple(cast(Iterable[object], pack)) == ("complete", *bounds)
    after_first = observations()
    assert main.backfill_pack_ranges() == 0
    assert observations() == after_first


@pytest.mark.parametrize("label", ["v01-v09", "v01-v08 Complete Series"])
def test_grabbed_complete_range_survives_two_startup_backfills(label: str) -> None:
    with shared.get_db() as db:
        _ = db.execute("UPDATE volumes SET status='wanted' WHERE id=2")
    assert asyncio.run(grab_core.grab_item(release(label), 1))
    before = observations()
    assert main.backfill_pack_ranges() == 0
    assert main.backfill_pack_ranges() == 0
    assert observations() == before
    assert statuses("volumes", "volume_num")[10] == "wanted"
    assert not metadata_enrichment._coverage_already_grabbed(
        1, "volume", (9, 10), None, {}, 100, 10
    )


def test_startup_unbounded_complete_keeps_legacy_claim_semantics() -> None:
    with shared.get_db() as db:
        _ = db.execute("UPDATE volumes SET monitored=0 WHERE id=10")
        _ = db.execute(
            "INSERT INTO volumes(series_id,status,torrent_name)"
            + " VALUES(1,'grabbed','Test Series Complete Series')"
        )
    assert main.backfill_pack_ranges() == 1
    assert statuses("volumes", "volume_num")[10] == "grabbed"
    before = observations()
    assert main.backfill_pack_ranges() == 0
    assert observations() == before
    assert metadata_enrichment._coverage_already_grabbed(
        1, "volume", (11, 12), None, {}, 100, 10
    )


def test_startup_chapter_pack_clears_false_volume_bounds() -> None:
    with shared.get_db() as db:
        _ = db.execute("UPDATE volumes SET status='wanted' WHERE id=3")
        _ = db.execute(
            "UPDATE series SET chapter_vol_map=?",
            (json.dumps({"10": 3, "20": 3, "30": 3}),),
        )
        _ = db.execute(
            "INSERT INTO volumes(series_id,status,torrent_name,vol_range_start,vol_range_end)"
            + " VALUES(1,'grabbed','Test Series 010-030',10,30)"
        )
    assert main.backfill_pack_ranges() == 1
    assert statuses("volumes", "volume_num")[3] == "grabbed"
    assert statuses("volumes", "volume_num")[10] == "wanted"
    with shared.get_db() as db:
        pack = cast(
            Row,
            db.execute(
                "SELECT pack_type,vol_range_start,vol_range_end FROM volumes WHERE volume_num IS NULL"
            ).fetchone(),
        )
        assert tuple(cast(Iterable[object], pack)) == ("chapter", None, None)
    before = observations()
    assert main.backfill_pack_ranges() == 0
    assert observations() == before


@pytest.mark.parametrize("number", [2, 3.5])
def test_numbered_complete_import_only_covers_its_own_volume(number: float) -> None:
    from import_commit import _process_volume_import
    from import_plan import _FilePlan, _ImportPlan

    file = _FilePlan(
        file_id=1,
        src_path="/fixture/source.cbz",
        filename="Test Series Vol 2.cbz",
        dst_path="/fixture/imported.cbz",
        import_kind="volume",
        file_type="volume",
        proposed_vol=number,
        proposed_chap=None,
        chap_range_end=None,
        vol_range_start=None,
        vol_range_end=None,
        pack_type="complete",
        is_special=0,
        special_title=None,
        has_volume_range=False,
        is_legacy_chapter_stub=False,
        is_legacy_chapter_recheck=False,
        plan_status="ready",
        plan_failure_reason="",
    )
    plan = _ImportPlan(
        queue={
            "download_id": "fixture",
            "torrent_url": "https://indexer.invalid/import",
        },
        series=None,
        series_tags=[],
        dst_dir="/fixture",
        import_mode="copy",
        now_ts=None,
        files=[file],
        series_id=1,
    )
    with shared.get_db() as db:
        _ = db.execute("DELETE FROM chapters WHERE volume_id=2")
        _ = db.execute("DELETE FROM volumes WHERE id=2")
        _process_volume_import(db, file, file.dst_path, plan, plan.queue, 1, set())
        imported = cast(
            Row,
            db.execute(
                "SELECT volume_num,status,pack_type,vol_range_start,vol_range_end"
                + " FROM volumes WHERE volume_num=?",
                (number,),
            ).fetchone(),
        )
        assert tuple(cast(Iterable[object], imported)) == (
            number,
            "downloaded",
            "complete",
            None,
            None,
        )
    before = observations()
    assert metadata_enrichment._coverage_already_grabbed(
        1, "volume", (number, number), None, {}, 100, 10
    )
    assert not metadata_enrichment._coverage_already_grabbed(
        1, "volume", (9, 10), None, {}, 100, 10
    )
    assert not metadata_enrichment._coverage_already_grabbed(
        1, "chapter", None, (90, 100), {"90": 9, "95": 10, "100": 10}, 100, 10
    )
    assert observations() == before
    assert asyncio.run(grab_core.grab_item(release("v09-v10"), 1))
    assert statuses("volumes", "volume_num")[10] == "grabbed"
