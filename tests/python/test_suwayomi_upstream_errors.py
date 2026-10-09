"""Upstream queue evidence and refresh failures must not invent library state."""

import asyncio
import json
import sqlite3
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient


@dataclass
class UpstreamJobs:
    db_path: Path
    library: Path
    manga_dir: Path
    nodes: list[dict[str, Any]] = field(default_factory=list)
    queue_data: dict[str, Any] = field(
        default_factory=lambda: {"downloadStatus": {"state": "STOPPED", "queue": []}}
    )
    queries: list[str] = field(default_factory=list)
    queue_failures: int = 0
    refreshes: list[int] = field(default_factory=list)

    def rows(self, sql: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql).fetchall()]

    def job(self) -> dict[str, Any]:
        return self.rows("SELECT * FROM suwayomi_downloads WHERE id=1")[0]

    def seed(self, ids: list[int], kind: str = "chapter") -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO suwayomi_downloads(id,series_id,suwayomi_manga_id,"
                " chapter_ids,chapter_num,volume_num,status,total)"
                " VALUES(1,1,101,?,?,?,'queued',?)",
                (
                    json.dumps(ids),
                    1 if kind == "chapter" else None,
                    1 if kind == "volume" else None,
                    len(ids),
                ),
            )

    def preserved(self) -> dict[str, Any]:
        tables = (
            "series",
            "chapters",
            "volumes",
            "mangadex_chapters",
            "series_metadata_fields",
            "suwayomi_sources",
            "settings",
            "history",
            "events",
        )
        return {
            **{table: self.rows(f"SELECT * FROM {table}") for table in tables},
            "library": {p.name: p.read_bytes() for p in self.library.iterdir()},
            "source": {p.name: p.read_bytes() for p in self.manga_dir.iterdir()},
        }

    async def gql(
        self,
        _client: dict[str, Any],
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.queries.append(query)
        if "fetchChapters" in query:
            assert variables is not None
            mid = variables["mid"]
            self.refreshes.append(mid)
            if mid == 101:
                raise RuntimeError("GraphQL: java.lang.Exception: No chapters found")
            return {"fetchChapters": {"chapters": []}}
        assert query.lstrip().startswith("query"), "Polling must never mutate upstream"
        if "downloadStatus" in query:
            if self.queue_failures:
                self.queue_failures -= 1
                raise httpx.ReadTimeout("private-provider-credential")
            return self.queue_data
        assert "manga(id:" in query
        return {"manga": {"title": "Upstream", "chapters": {"nodes": self.nodes}}}


def node(cid: int, downloaded: bool = False) -> dict[str, Any]:
    return {
        "id": cid,
        "isDownloaded": downloaded,
        "chapterNumber": cid,
        "name": f"Vol.1 Ch.{cid}",
        "scanlator": None,
    }


def entry(cid: Any, state: str = "ERROR") -> dict[str, Any]:
    return {"chapter": {"id": cid}, "state": state}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[UpstreamJobs]:
    import main
    import security
    import shared
    from routers import suwayomi_ as swy

    db_path = tmp_path / "upstream.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    monkeypatch.setattr(main, "CONFIG", {})
    monkeypatch.setattr(shared, "CONFIG", {})
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    main.init_db()
    main.load_config()
    library = tmp_path / "library"
    library.mkdir()
    (library / "retained.cbz").write_bytes(b"retained local content")
    swy_root = tmp_path / "swy"
    manga_dir = swy_root / "mangas" / "Source" / "Upstream"
    manga_dir.mkdir(parents=True)
    with zipfile.ZipFile(manga_dir / "Vol.1 Ch.1.cbz", "w") as cbz:
        cbz.writestr("0001.png", b"source page")
    monkeypatch.setattr(main, "_series_library_dir", lambda db, sid: str(library))
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO download_clients(id,name,type,host,enabled,download_path)"
            " VALUES(1,'swy','suwayomi','http://swy.invalid',1,?)",
            (str(swy_root),),
        )
        for sid in (1, 2):
            db.execute(
                "INSERT INTO series(id,title,search_pattern,status,monitored,"
                " total_chapters,total_volumes,chapter_vol_map,chapter_map_source)"
                " VALUES(?,'Upstream','Upstream','FINISHED',1,30,3,?, 'mangadex')",
                (sid, '{"1":1,"2":1}'),
            )
            db.execute(
                "INSERT INTO suwayomi_sources(series_id,source_id,source_name,"
                " suwayomi_manga_id) VALUES(?,'source','Source',?)",
                (sid, 100 + sid),
            )
        db.execute(
            "INSERT INTO chapters(id,series_id,chapter_num,status,monitored,torrent_name)"
            " VALUES(1,1,1,'grabbed',0,'retained provenance')"
        )
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,monitored,import_path)"
            " VALUES(1,1,1,'grabbed',0,?)",
            (str(library / "retained.cbz"),),
        )
        db.execute(
            "INSERT INTO mangadex_chapters(series_id,mangadex_chapter_id,chapter_num,volume_num)"
            " VALUES(1,'cached-source-id',1,1)"
        )
        db.execute(
            "INSERT INTO series_metadata_fields(series_id,field_name,value_json,"
            " selected_source,selected_at) VALUES(1,'chapter_vol_map',?,'mangadex','2026-01-01')",
            ('{"1":1,"2":1}',),
        )
    result = UpstreamJobs(db_path, library, manga_dir)
    monkeypatch.setattr(swy, "_gql", result.gql)
    yield result


def poll() -> None:
    from routers import suwayomi_ as swy

    asyncio.run(swy.check_suwayomi_jobs())


@pytest.mark.parametrize("kind", ["chapter", "volume"])
def test_tracked_upstream_error_is_visible_without_import_or_retry(
    env: UpstreamJobs,
    kind: str,
) -> None:
    env.seed([1], kind)
    env.nodes = [node(1)]
    env.queue_data["downloadStatus"]["queue"] = [entry(1)]
    before = env.preserved()
    poll()
    job = env.job()
    assert job["status"] == "error"
    assert "upstream download ERROR" in job["error"]
    assert job["progress"] == 0 and job["total"] == 1
    assert job["chapter_ids"] == "[1]"
    assert env.preserved() == before
    assert len(env.queries) == 2
    import main

    client = TestClient(main.app)
    try:
        response = client.get("/queue/table")
    finally:
        client.close()
    assert response.status_code == 200
    assert 'hx-post="/api/suwayomi/jobs/1/retry"' in response.text
    assert "upstream download ERROR" in response.text


@pytest.mark.parametrize("state", ["DOWNLOADING", "QUEUED"])
@pytest.mark.parametrize("downloader", ["STARTED", "STOPPED"])
def test_normal_queue_or_intentional_pause_is_not_an_error(
    env: UpstreamJobs,
    state: str,
    downloader: str,
) -> None:
    env.seed([1])
    env.nodes = [node(1)]
    env.queue_data = {
        "downloadStatus": {"state": downloader, "queue": [entry(1, state)]}
    }
    before = env.preserved()
    poll()
    assert env.job()["status"] == "queued" and env.job()["error"] is None
    assert env.preserved() == before


@pytest.mark.parametrize(
    "queue_data",
    [
        {},
        {"downloadStatus": None},
        {"downloadStatus": {}},
        {"downloadStatus": {"state": "STOPPED", "queue": []}},
        {"downloadStatus": {"queue": None}},
    ],
)
def test_empty_or_unavailable_queue_is_not_terminal_evidence(
    env: UpstreamJobs,
    queue_data: dict[str, Any],
) -> None:
    env.seed([1])
    env.nodes = [node(1)]
    env.queue_data = queue_data
    before = env.preserved()
    poll()
    assert env.job()["status"] == "queued" and env.job()["error"] is None
    assert env.preserved() == before


@pytest.mark.parametrize("cid", [99, True, "not-an-id", None])
def test_untracked_or_malformed_queue_error_is_ignored(
    env: UpstreamJobs, cid: Any
) -> None:
    env.seed([1])
    env.nodes = [node(1)]
    env.queue_data["downloadStatus"]["queue"] = [entry(cid), {}, {"state": "ERROR"}]
    before = env.preserved()
    poll()
    assert env.job()["status"] == "queued" and env.job()["error"] is None
    assert env.preserved() == before


@pytest.mark.parametrize("feed", [[], [{"id": 1}], [{"id": 1, "isDownloaded": None}]])
def test_missing_feed_download_flag_is_not_terminal_evidence(
    env: UpstreamJobs,
    feed: list[dict[str, Any]],
) -> None:
    env.seed([1])
    env.nodes = feed
    env.queue_data["downloadStatus"]["queue"] = [entry(1)]
    before = env.preserved()
    poll()
    assert env.job()["status"] == "queued" and env.job()["error"] is None
    assert env.preserved() == before


def test_partial_progress_survives_tracked_error(env: UpstreamJobs) -> None:
    env.seed([1, 2], "volume")
    env.nodes = [node(1, True), node(2)]
    env.queue_data["downloadStatus"]["queue"] = [entry(1), entry(2), entry(2)]
    before = env.preserved()
    poll()
    job = env.job()
    assert job["status"] == "error" and job["progress"] == 1
    assert "1 tracked chapter" in job["error"]
    assert job["chapter_ids"] == "[1, 2]" and job["total"] == 2
    assert env.preserved() == before


def test_downloaded_chapter_wins_over_stale_error_while_other_is_pending(
    env: UpstreamJobs,
) -> None:
    env.seed([1, 2], "volume")
    env.nodes = [node(1, True), node(2)]
    env.queue_data["downloadStatus"]["queue"] = [entry(1), entry(2, "QUEUED")]
    before = env.preserved()
    poll()
    assert env.job()["status"] == "queued" and env.job()["progress"] == 1
    assert env.job()["error"] is None
    assert env.preserved() == before


@pytest.mark.parametrize("kind", ["chapter", "volume"])
def test_all_downloaded_imports_without_queue_query(
    env: UpstreamJobs, kind: str
) -> None:
    env.seed([1], kind)
    env.nodes = [node(1, True)]
    env.queue_failures = 10
    poll()
    assert env.job()["status"] == "completed" and env.job()["progress"] == 1
    assert len(env.queries) == 1
    assert (env.library / "retained.cbz").read_bytes() == b"retained local content"
    assert (env.manga_dir / "Vol.1 Ch.1.cbz").is_file()


@pytest.mark.parametrize("failures", [1, 2, 3])
def test_queue_query_failure_uses_bounded_retry_without_import_or_secrets(
    env: UpstreamJobs,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failures: int,
) -> None:
    from routers import suwayomi_ as swy

    env.seed([1])
    env.nodes = [node(1)]
    env.queue_failures = failures
    before = env.preserved()
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(swy._aio, "sleep", sleep)
    poll()
    job = env.job()
    assert len(env.queries) == 2 * min(failures + 1, 3)
    assert sleeps == ([2] if failures == 1 else [2, 4])
    assert job["status"] == ("error" if failures == 3 else "queued")
    if failures == 3:
        assert "download queue unavailable (ReadTimeout)" in job["error"]
        assert "upstream download ERROR" not in job["error"]
    else:
        assert job["error"] is None
    assert "private-provider-credential" not in caplog.text
    assert "private-provider-credential" not in (job["error"] or "")
    assert env.preserved() == before


def test_refresh_exception_preserves_cache_jobs_and_local_data(
    env: UpstreamJobs,
) -> None:
    from routers import suwayomi_ as swy

    env.seed([1])
    before = env.preserved()
    job_before = env.job()
    series = env.rows("SELECT * FROM series WHERE id=1")[0]
    with pytest.raises(RuntimeError, match="No chapters found"):
        asyncio.run(swy._suwayomi_sync_series({}, series))
    assert env.preserved() == before and env.job() == job_before
    assert env.refreshes == [101]


def test_monitor_continues_after_refresh_exception_without_data_changes(
    env: UpstreamJobs,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from routers import suwayomi_ as swy

    env.seed([1])
    before = env.preserved()
    job_before = env.job()

    async def sleep(delay: float) -> None:
        if delay >= 3600:
            raise asyncio.CancelledError

    monkeypatch.setattr(swy._aio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(swy.suwayomi_monitor_loop())
    assert env.refreshes == [101, 102]
    assert env.preserved() == before and env.job() == job_before
    assert "suwayomi_monitor series 1" in caplog.text
