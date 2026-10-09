"""Issue #381: grabbed chapters remain observable through startup recovery."""

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
class Suwayomi:
    db_path: Path
    library: Path
    downloaded: list[bool | None] = field(default_factory=lambda: [True, True])
    startup_error: Exception | None = None
    enqueue_error: Exception | None = None
    poll_error: Exception | None = None
    polls: int = 0
    starts: int = 0
    enqueues: list[list[int]] = field(default_factory=list)
    persisted_at_start: list[bool] = field(default_factory=list)

    def job(self) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM suwayomi_downloads").fetchone()
        return dict(row) if row else None

    def item(self, kind: str) -> dict[str, Any]:
        table = "volumes" if kind == "volume" else "chapters"
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(f"SELECT * FROM {table} WHERE id=1").fetchone()
        assert row is not None
        return dict(row)

    async def gql(
        self, _client: dict, query: str, variables: dict | None = None
    ) -> dict:
        chapters = [
            {
                "id": i,
                "chapterNumber": float(i),
                "name": f"Vol.1 Ch.{i}",
                "sourceOrder": i,
                **({"isDownloaded": done} if done is not None else {}),
            }
            for i, done in enumerate(self.downloaded, start=1)
        ]
        if "fetchChapters" in query:
            return {"fetchChapters": {"chapters": chapters}}
        if "enqueueChapterDownloads" in query:
            assert variables is not None
            self.enqueues.append(variables["ids"])
            if self.enqueue_error:
                raise self.enqueue_error
            return {"enqueueChapterDownloads": {"clientMutationId": None}}
        if "startDownloader" in query:
            self.starts += 1
            self.persisted_at_start.append(self.job() is not None)
            if self.startup_error:
                raise self.startup_error
            return {"startDownloader": {"clientMutationId": None}}
        if "manga(id:" in query:
            self.polls += 1
            if self.poll_error:
                raise self.poll_error
            for chapter in chapters:
                chapter["isDownloaded"] = bool(chapter.get("isDownloaded"))
            return {
                "manga": {
                    "title": "Recovery",
                    "source": {"displayName": "MangaDex"},
                    "chapters": {"nodes": chapters},
                }
            }
        raise AssertionError(f"Unexpected GraphQL operation: {query}")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Suwayomi]:
    import main
    import security
    import shared
    from routers import suwayomi_ as swy

    db_path = tmp_path / "test.db"
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
    monkeypatch.setattr(main, "_series_library_dir", lambda db, sid: str(library))
    swy_root = tmp_path / "swy"
    manga_dir = swy_root / "mangas" / "MangaDex" / "Recovery"
    manga_dir.mkdir(parents=True)
    for number in (1, 2):
        with zipfile.ZipFile(manga_dir / f"Vol.1 Ch.{number}.cbz", "w") as cbz:
            cbz.writestr("0001.png", b"test page")

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO download_clients(id, name, type, host, enabled, download_path)"
            " VALUES(1, 'swy', 'suwayomi', 'http://swy.invalid', 1, ?)",
            (str(swy_root),),
        )
        db.execute(
            "INSERT INTO series(id, title, search_pattern) VALUES(7, 'Recovery', 'Recovery')"
        )
        db.execute(
            "INSERT INTO suwayomi_sources(series_id, source_id, source_name, suwayomi_manga_id)"
            " VALUES(7, 'md', 'MangaDex', 101)"
        )
        db.execute(
            "INSERT INTO volumes(id, series_id, volume_num, status) VALUES(1, 7, 1, 'wanted')"
        )
        db.execute(
            "INSERT INTO chapters(id, series_id, chapter_num, status) VALUES(1, 7, 1, 'wanted')"
        )

    upstream = Suwayomi(db_path, library)
    monkeypatch.setattr(swy, "_gql", upstream.gql)
    yield upstream


def grab(kind: str) -> bool:
    from routers import suwayomi_ as swy

    workflow = swy.suwayomi_grab if kind == "volume" else swy.suwayomi_chapter_grab
    return asyncio.run(workflow(7, 1.0))


def poll() -> None:
    from routers import suwayomi_ as swy

    asyncio.run(swy.check_suwayomi_jobs())


def seed_failed_job(env: Suwayomi, kind: str) -> None:
    table, column, ids = (
        ("volumes", "volume_num", "[1,2]")
        if kind == "volume"
        else ("chapters", "chapter_num", "[1]")
    )
    with sqlite3.connect(env.db_path) as db:
        db.execute(f"UPDATE {table} SET status='grabbed', client='suwayomi'")
        db.execute(
            f"INSERT INTO suwayomi_downloads(series_id, {column}, suwayomi_manga_id,"
            " chapter_ids, total, status, error) VALUES(7, 1, 101, ?, ?, 'error', 'old')",
            (ids, len(json.loads(ids))),
        )


@pytest.mark.parametrize("kind", ["volume", "chapter"])
def test_already_downloaded_grab_skips_start_and_imports(
    env: Suwayomi, kind: str
) -> None:
    # An unrelated new chapter must not affect a single-chapter grab.
    env.downloaded = [True, True] if kind == "volume" else [True, False]
    env.startup_error = RuntimeError("Timed out waiting for 30000 ms")
    assert grab(kind) is True
    job = env.job()
    assert job is not None
    assert job["status"] == "queued"
    assert job["total"] == (2 if kind == "volume" else 1)
    assert json.loads(job["chapter_ids"]) == ([1, 2] if kind == "volume" else [1])
    assert env.starts == 0
    assert env.item(kind)["status"] == "grabbed"
    poll()
    job = env.job()
    assert job is not None and job["status"] == "completed"
    assert env.item(kind)["status"] == "downloaded"
    assert Path(env.item(kind)["import_path"]).is_file()


@pytest.mark.parametrize("kind", ["volume", "chapter"])
@pytest.mark.parametrize("downloaded", [False, None])
def test_new_or_unknown_grab_persists_before_start(
    env: Suwayomi, kind: str, downloaded: bool | None
) -> None:
    env.downloaded = [True, downloaded] if kind == "volume" else [downloaded, True]
    assert grab(kind) is True
    assert env.starts == 1
    assert env.persisted_at_start == [True]
    poll()
    job = env.job()
    assert job is not None and job["status"] == "queued"
    assert job["error"] is None
    assert job["progress"] == (1 if kind == "volume" else 0)


@pytest.mark.parametrize("kind", ["volume", "chapter"])
@pytest.mark.parametrize("completed_after_start", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, httpx.ReadTimeout])
def test_startup_failure_is_tracked_then_imported_or_visible_error(
    env: Suwayomi, kind: str, completed_after_start: bool, error_type: type[Exception]
) -> None:
    env.downloaded = [False, False]
    env.startup_error = error_type("startup failed")
    assert grab(kind) is True
    job = env.job()
    assert job is not None and job["status"] == "queued"
    assert "startup failed" in job["error"]
    assert error_type.__name__ in job["error"]
    assert env.persisted_at_start == [True]
    if completed_after_start:
        env.downloaded = [True, True]
    poll()
    job = env.job()
    assert job is not None
    assert job["status"] == ("completed" if completed_after_start else "error")
    assert env.item(kind)["status"] == (
        "downloaded" if completed_after_start else "grabbed"
    )
    if completed_after_start:
        assert job["error"] is None
    else:
        assert "startup failed" in job["error"]
        from routers import suwayomi_ as swy

        downloads = json.loads(bytes(asyncio.run(swy.list_downloads()).body))[
            "downloads"
        ]
        assert downloads[0]["status"] == "error"
        assert "startup failed" in downloads[0]["error"]
        import main

        client = TestClient(main.app)
        response = client.get("/queue/table")
        client.close()
        assert response.status_code == 200
        assert "startup failed" in response.text
        assert 'hx-post="/api/suwayomi/jobs/1/retry"' in response.text


@pytest.mark.parametrize("kind", ["volume", "chapter"])
def test_enqueue_failure_creates_no_job_or_grab_metadata(
    env: Suwayomi, kind: str
) -> None:
    env.enqueue_error = RuntimeError("enqueue rejected")
    assert grab(kind) is False
    assert env.job() is None
    assert env.starts == 0
    assert env.item(kind)["status"] == "wanted"
    assert env.item(kind)["grabbed_at"] is None


@pytest.mark.parametrize("kind", ["volume", "chapter"])
def test_retry_known_complete_skips_start_and_imports(env: Suwayomi, kind: str) -> None:
    import main

    seed_failed_job(env, kind)
    env.startup_error = RuntimeError("empty queue timeout")
    client = TestClient(main.app, headers={"X-Api-Key": main.get_cfg("api_key")})
    response = client.post("/api/suwayomi/jobs/1/retry", headers={"HX-Request": "true"})
    client.close()
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert env.starts == 0
    poll()
    job = env.job()
    assert job is not None and job["status"] == "completed"
    assert env.item(kind)["status"] == "downloaded"


@pytest.mark.parametrize("kind", ["volume", "chapter"])
def test_retry_startup_failure_reapplies_observable_policy(
    env: Suwayomi, kind: str
) -> None:
    from routers import suwayomi_ as swy

    seed_failed_job(env, kind)
    env.downloaded = [False, False]
    env.startup_error = RuntimeError("retry startup failed")
    response = asyncio.run(swy.retry_suwayomi_job(1))
    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {"ok": True}
    job = env.job()
    assert job is not None and isinstance(job["error"], str)
    assert "retry startup failed" in job["error"]
    poll()
    job = env.job()
    assert job is not None and job["status"] == "error"


@pytest.mark.parametrize("kind", ["volume", "chapter"])
def test_startup_and_poll_exhaustion_recovers_through_visible_manual_retry(
    env: Suwayomi, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import main
    from routers import suwayomi_ as swy

    env.downloaded = [False, False]
    env.startup_error = RuntimeError("startup failed")
    assert grab(kind) is True
    job = env.job()
    assert job is not None and job["status"] == "queued"
    assert "startup failed" in job["error"]
    job_id = job["id"]

    env.poll_error = RuntimeError("provider poll unavailable")
    delays: list[float] = []

    async def no_delay(seconds: float) -> None:
        delays.append(seconds)

    with monkeypatch.context() as patcher:
        patcher.setattr(swy._aio, "sleep", no_delay)
        poll()

    assert env.polls == 3
    assert delays == [2, 4]
    job = env.job()
    assert job is not None and job["id"] == job_id
    assert job["status"] == "error"
    assert "RuntimeError: provider poll unavailable" in job["error"]

    client = TestClient(main.app, headers={"X-Api-Key": main.get_cfg("api_key")})
    response = client.get("/queue/table")
    assert response.status_code == 200
    assert "provider poll unavailable" in response.text
    assert f'hx-post="/api/suwayomi/jobs/{job_id}/retry"' in response.text
    assert "Queue is empty" not in response.text

    env.poll_error = None
    env.downloaded = [True, True]
    poll()
    assert env.polls == 3  # Errored jobs require an explicit manual retry.
    job = env.job()
    assert job is not None and job["status"] == "error"

    response = client.post(
        f"/api/suwayomi/jobs/{job_id}/retry", headers={"HX-Request": "true"}
    )
    client.close()
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert env.starts == 1  # Only the original failed startup; cached retry skips it.
    job = env.job()
    assert job is not None and job["id"] == job_id
    assert job["status"] == "queued"
    assert job["error"] is None

    poll()
    assert env.polls == 4
    job = env.job()
    assert job is not None and job["status"] == "completed"
    assert job["error"] is None
    assert env.item(kind)["status"] == "downloaded"
    assert Path(env.item(kind)["import_path"]).is_file()


@pytest.mark.parametrize("kind", ["volume", "chapter"])
def test_retry_enqueue_failure_stays_visible_and_retryable(
    env: Suwayomi, kind: str
) -> None:
    from routers import suwayomi_ as swy

    seed_failed_job(env, kind)
    env.enqueue_error = RuntimeError("retry enqueue failed")
    response = asyncio.run(swy.retry_suwayomi_job(1))
    assert response.status_code == 200
    job = env.job()
    assert job is not None and job["status"] == "error"
    assert "retry enqueue failed" in job["error"]
    assert env.starts == 0


@pytest.mark.parametrize("startup_failure", [False, True])
def test_volume_api_acknowledges_durable_job(
    env: Suwayomi, startup_failure: bool
) -> None:
    import main

    if startup_failure:
        env.downloaded = [False, False]
    env.startup_error = RuntimeError("start unavailable")
    client = TestClient(main.app, headers={"X-Api-Key": main.get_cfg("api_key")})
    response = client.post("/api/series/7/suwayomi/grab/1.0")
    client.close()
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    job = env.job()
    assert job is not None and job["status"] == "queued"


def test_volume_api_enqueue_failure_preserves_http500(env: Suwayomi) -> None:
    import main

    env.enqueue_error = RuntimeError("enqueue rejected")
    client = TestClient(main.app, headers={"X-Api-Key": main.get_cfg("api_key")})
    response = client.post("/api/series/7/suwayomi/grab/1.0")
    client.close()
    assert response.status_code == 500
    assert response.json()["ok"] is False
    assert env.job() is None


@pytest.mark.parametrize("kind", ["volume", "chapter"])
def test_suwayomi_only_startup_error_renders_retry(env: Suwayomi, kind: str) -> None:
    from routers import queue_
    from routers._templates import templates

    env.downloaded = [False, False]
    env.startup_error = RuntimeError("startup failed")
    assert grab(kind) is True
    poll()
    _, _, category, suwayomi_rows = asyncio.run(queue_._build_queue_rows())
    assert len(suwayomi_rows) == 1
    assert suwayomi_rows[0]["status"] == "error"

    html = templates.env.get_template("partials/queue_table.html").render(
        queue_rows=[],
        suwayomi_rows=suwayomi_rows,
        configured_category=category,
        queue_status=None,
    )
    assert "startup failed" in html
    assert 'hx-post="/api/suwayomi/jobs/1/retry"' in html
    assert "Queue is empty" not in html
    assert "<th>Release</th>" not in html
    assert html.count("<table ") == 1


@pytest.mark.parametrize("normal_queue", [False, True])
@pytest.mark.parametrize("suwayomi_queue", [False, True])
def test_queue_empty_state_and_mixed_queues(
    env: Suwayomi, normal_queue: bool, suwayomi_queue: bool
) -> None:
    import main

    if normal_queue:
        with sqlite3.connect(env.db_path) as db:
            db.execute(
                "INSERT INTO pending_releases(series_id, url, title, indexer, protocol)"
                " VALUES(7, 'https://tracker.invalid/release', 'Torrent Release', 'Test', 'torrent')"
            )
    if suwayomi_queue:
        assert grab("chapter") is True

    client = TestClient(main.app)
    response = client.get("/queue/table")
    client.close()
    assert response.status_code == 200
    assert ("Queue is empty" in response.text) is not (normal_queue or suwayomi_queue)
    assert ("Torrent Release" in response.text) is normal_queue
    assert ("Suwayomi Downloads" in response.text) is suwayomi_queue
    assert response.text.count("<table ") == int(normal_queue) + int(suwayomi_queue)
