"""Regression tests for automatic backlog title matching."""

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401

import pytest

import grab_backlog
from grab_backlog import matches

ReleaseItem = dict[str, object]


def test_backlog_does_not_match_alias_inside_uploader_tag():
    release = "Unrelated Comic 002 (2024) (Digital) (Wanpanman-Empire) (cbz)"

    assert not matches("Wanpanman", release)


def test_backlog_matches_alias_as_the_release_title():
    assert matches("Wanpanman", "Wanpanman v01 (Digital) (cbz)")


def test_backlog_matches_primary_title_with_release_metadata():
    assert matches(
        "One-Punch Man",
        "One-Punch Man v01 (2015) (Digital) (cbz)",
    )


def test_grab_existing_ignores_uploader_alias_and_grabs_title_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Only the release whose series title is the alias reaches grab_item."""
    import events
    import shared

    db_path = tmp_path / "backlog-matching.db"
    with sqlite3.connect(db_path) as db:
        _ = db.executescript(
            """
            CREATE TABLE series (
                id INTEGER PRIMARY KEY,
                status TEXT,
                total_volumes INTEGER
            );
            CREATE TABLE seen (torrent_url TEXT);
            CREATE TABLE blocklist (torrent_url TEXT);
            CREATE TABLE series_aliases (series_id INTEGER, alias TEXT);
            INSERT INTO series (id, status, total_volumes)
            VALUES (7, 'RELEASING', NULL);
            INSERT INTO series_aliases (series_id, alias)
            VALUES (7, 'Wanpanman');
            """
        )

    unrelated_release: ReleaseItem = {
        "url": "https://example.test/unrelated",
        "title": "Unrelated Comic 002 (2024) (Digital) (Wanpanman-Empire) (cbz)",
    }
    matching_release: ReleaseItem = {
        "url": "https://example.test/wanpanman",
        "title": "Wanpanman v01 (Digital) (cbz)",
    }
    search_queries: list[tuple[str, int]] = []
    grab_calls: list[tuple[ReleaseItem, int]] = []

    async def fake_search(query: str, *, series_id: int) -> list[ReleaseItem]:
        search_queries.append((query, series_id))
        if query == "One-Punch Man":
            return [unrelated_release, matching_release]
        return []

    async def fake_grab(item: ReleaseItem, series_id: int) -> bool:
        grab_calls.append((item, series_id))
        return True

    def ignore_log_event(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    monkeypatch.setattr(grab_backlog, "_search_all", fake_search)
    monkeypatch.setattr(grab_backlog, "grab_item", fake_grab)
    monkeypatch.setattr(events, "log_event", ignore_log_event)

    grabbed = asyncio.run(
        grab_backlog._grab_existing_inner(
            series_id=7,
            title="One-Punch Man",
            pattern="One-Punch Man",
        )
    )

    assert grabbed == 1
    assert search_queries == [("One-Punch Man", 7), ("Wanpanman", 7)]
    assert grab_calls == [(matching_release, 7)]
