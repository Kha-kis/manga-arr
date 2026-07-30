"""End-to-end regressions for downloader-local ID ownership."""

from __future__ import annotations

import asyncio
import sqlite3
import zipfile
from pathlib import Path
from typing import Any, Literal

import pytest


@pytest.fixture
def ownership_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    import import_execute
    import main
    import security
    import shared

    db_path = tmp_path / "ownership.db"
    library = tmp_path / "library"
    downloads = tmp_path / "downloads"
    library.mkdir()
    downloads.mkdir()

    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(shared, "DB_PATH", str(db_path))
    security._SECRET_CIPHER = None
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    main.init_db()
    main.load_config()
    monkeypatch.setattr(import_execute, "_IMPORT_SEM", None)
    for config in (main.CONFIG, shared.CONFIG):
        monkeypatch.setitem(config, "save_path", str(library))
        monkeypatch.setitem(config, "import_mode", "copy")
        monkeypatch.setitem(config, "remove_completed", "false")

    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM root_folders")
        db.execute(
            "INSERT INTO root_folders(id,path,label,is_default)"
            " VALUES(1,?,'Ownership',1)",
            (str(library),),
        )
    return {
        "db_path": db_path,
        "library": library,
        "downloads": downloads,
    }


def _archive(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("001.jpg", b"page")
    return path


def _seed_series(db: sqlite3.Connection, series_id: int, title: str) -> None:
    db.execute(
        "INSERT INTO series(id,title,search_pattern,root_folder_id)"
        " VALUES(?,?,?,1)",
        (series_id, title, title),
    )


class _Response:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        data: Any = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._data = data

    def json(self) -> Any:
        return self._data


def test_discovery_polls_every_exact_qbit_and_sab_owner(
    ownership_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tagged/non-priority owners with colliding local IDs remain independent."""
    import import_discovery
    from routers import suwayomi_ as suwayomi_router

    db_path = ownership_env["db_path"]
    downloads = ownership_env["downloads"]
    paths = {
        "qbit-a.invalid": _archive(downloads / "qbit-a" / "Qbit A v01.cbz"),
        "qbit-b.invalid": _archive(downloads / "qbit-b" / "Qbit B v01.cbz"),
        "sab-a.invalid": _archive(downloads / "sab-a" / "Sab A v01.cbz"),
        "sab-b.invalid": _archive(downloads / "sab-b" / "Sab B v01.cbz"),
    }
    with sqlite3.connect(db_path) as db:
        clients = (
            (101, "qBit priority", "qbittorrent", "http://qbit-a.invalid", "qa", "qp", 1),
            (102, "qBit tagged", "qbittorrent", "http://qbit-b.invalid", "qb", "qt", 99),
            (201, "SAB priority", "sabnzbd", "http://sab-a.invalid", "", "sa", 1),
            (202, "SAB tagged", "sabnzbd", "http://sab-b.invalid", "", "sb", 99),
        )
        db.executemany(
            "INSERT INTO download_clients("
            "id,name,type,host,username,password,enabled,priority,category"
            ") VALUES(?,?,?,?,?,?,1,?,'manga')",
            clients,
        )
        db.executemany(
            "INSERT INTO download_client_tags(client_id,tag) VALUES(?,?)",
            ((102, "tagged-qbit"), (202, "tagged-sab")),
        )
        for series_id, title in (
            (1, "Qbit A"),
            (2, "Qbit B"),
            (3, "Sab A"),
            (4, "Sab B"),
        ):
            _seed_series(db, series_id, title)
        db.executemany(
            "INSERT INTO seen("
            "torrent_url,torrent_name,series_id,volume_num,protocol,client,"
            "download_id,download_client_id,indexer"
            ") VALUES(?,?,?,1,?,?,?,?,?)",
            (
                (
                    "magnet:qbit-a",
                    "Qbit A v01",
                    1,
                    "torrent",
                    "qbittorrent",
                    "ABCDEF",
                    101,
                    "qbit-a-indexer",
                ),
                (
                    "magnet:qbit-b",
                    "Qbit B v01",
                    2,
                    "torrent",
                    "qbittorrent",
                    "abcdef",
                    102,
                    "qbit-b-indexer",
                ),
                (
                    "https://indexer.invalid/sab-a",
                    "Sab A v01",
                    3,
                    "nzb",
                    "sabnzbd",
                    "NZO-COLLISION",
                    201,
                    "sab-a-indexer",
                ),
                (
                    "https://indexer.invalid/sab-b",
                    "Sab B v01",
                    4,
                    "nzb",
                    "sabnzbd",
                    "NZO-COLLISION",
                    202,
                    "sab-b-indexer",
                ),
            ),
        )

    requests: list[tuple[str, str, dict[str, Any]]] = []

    class _Clients:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> _Clients:
            return self

        async def __aexit__(self, *args: object) -> bool:
            del args
            return False

        async def post(
            self,
            url: str,
            *args: object,
            **kwargs: Any,
        ) -> _Response:
            del args
            requests.append(("POST", url, dict(kwargs)))
            assert url.endswith("/api/v2/auth/login")
            return _Response(text="Ok.")

        async def get(
            self,
            url: str,
            *args: object,
            **kwargs: Any,
        ) -> _Response:
            del args
            requests.append(("GET", url, dict(kwargs)))
            host = url.split("//", 1)[1].split("/", 1)[0]
            if url.endswith("/api/v2/torrents/info"):
                title = "Qbit A" if host == "qbit-a.invalid" else "Qbit B"
                return _Response(
                    data=[
                        {
                            "hash": "ABCDEF",
                            "name": f"{title} v01",
                            "progress": 1.0,
                            "content_path": str(paths[host].parent),
                            "state": "uploading",
                        }
                    ]
                )
            params = kwargs["params"]
            if params["mode"] == "history":
                title = "Sab A" if host == "sab-a.invalid" else "Sab B"
                return _Response(
                    data={
                        "history": {
                            "slots": [
                                {
                                    "nzo_id": "NZO-COLLISION",
                                    "status": "Completed",
                                    "storage": str(paths[host].parent),
                                    "name": f"{title} v01",
                                }
                            ]
                        }
                    }
                )
            return _Response(data={"queue": {"slots": []}})

    scheduled: list[int] = []

    async def _no_suwayomi() -> None:
        return None

    monkeypatch.setattr(import_discovery.httpx, "AsyncClient", _Clients)
    monkeypatch.setattr(
        import_discovery,
        "schedule_import_worker",
        scheduled.append,
    )
    monkeypatch.setattr(suwayomi_router, "check_suwayomi_jobs", _no_suwayomi)
    monkeypatch.setattr(
        import_discovery,
        "get_cfg",
        lambda key, default="": {
            "blocklist_ttl_days": "0",
            "failed_download_handling": "0",
        }.get(key, default),
    )

    asyncio.run(import_discovery._check_download_status_impl())

    with sqlite3.connect(db_path) as db:
        queues = db.execute(
            "SELECT series_id,download_id,download_client_id,src_dir"
            " FROM import_queue ORDER BY series_id"
        ).fetchall()
    assert queues == [
        (1, "abcdef", 101, str(paths["qbit-a.invalid"].parent)),
        (2, "abcdef", 102, str(paths["qbit-b.invalid"].parent)),
        (3, "NZO-COLLISION", 201, str(paths["sab-a.invalid"].parent)),
        (4, "NZO-COLLISION", 202, str(paths["sab-b.invalid"].parent)),
    ]
    assert scheduled == [1, 2, 3, 4]
    auth_payloads = {
        url.split("//", 1)[1].split("/", 1)[0]: kwargs["data"]
        for method, url, kwargs in requests
        if method == "POST"
    }
    assert auth_payloads == {
        "qbit-a.invalid": {"username": "qa", "password": "qp"},
        "qbit-b.invalid": {"username": "qb", "password": "qt"},
    }
    sab_keys = {
        url.split("//", 1)[1].split("/", 1)[0]: kwargs["params"]["apikey"]
        for method, url, kwargs in requests
        if method == "GET" and kwargs["params"].get("mode") == "history"
    }
    assert sab_keys == {"sab-a.invalid": "sa", "sab-b.invalid": "sb"}


def test_ambiguous_legacy_owner_is_left_unpolled_with_health_signal(
    ownership_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_discovery
    from routers import suwayomi_ as suwayomi_router

    with sqlite3.connect(ownership_env["db_path"]) as db:
        _seed_series(db, 1, "Legacy")
        db.executemany(
            "INSERT INTO download_clients("
            "id,name,type,host,username,password,enabled,priority"
            ") VALUES(?,?,'qbittorrent',?,'u','p',1,?)",
            (
                (101, "qBit A", "http://qbit-a.invalid", 1),
                (102, "qBit B", "http://qbit-b.invalid", 2),
            ),
        )
        db.execute(
            "INSERT INTO seen("
            "torrent_url,torrent_name,series_id,volume_num,protocol,client,"
            "download_id,download_client_id"
            ") VALUES('magnet:legacy','Legacy v01',1,1,'torrent',"
            "'qbittorrent','LEGACY-HASH',NULL)"
        )

    class _NetworkMustNotStart:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("ambiguous ownerless row was polled")

    async def _no_suwayomi() -> None:
        return None

    monkeypatch.setattr(
        import_discovery.httpx,
        "AsyncClient",
        _NetworkMustNotStart,
    )
    monkeypatch.setattr(suwayomi_router, "check_suwayomi_jobs", _no_suwayomi)
    monkeypatch.setattr(
        import_discovery,
        "get_cfg",
        lambda key, default="": "0" if key == "blocklist_ttl_days" else default,
    )
    asyncio.run(import_discovery._check_download_status_impl())

    with sqlite3.connect(ownership_env["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
        event = db.execute(
            "SELECT event_type,message FROM events"
            " WHERE event_type='configuration_error' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert event is not None
    assert "ownerless" in event[1] and "ambiguous" in event[1]


def test_disabled_sab_client_keeps_ownerless_discovery_non_destructive(
    ownership_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One enabled client cannot claim legacy rows while another may own them."""
    import import_discovery
    from routers import suwayomi_ as suwayomi_router

    db_path = ownership_env["db_path"]
    with sqlite3.connect(db_path) as db:
        _seed_series(db, 1, "Legacy SAB")
        db.executemany(
            "INSERT INTO download_clients("
            "id,name,type,host,password,enabled,priority"
            ") VALUES(?,?,'sabnzbd',?,?,?,?)",
            (
                (
                    201,
                    "Enabled SAB",
                    "http://sab-enabled.invalid",
                    "enabled-secret",
                    1,
                    1,
                ),
                (
                    202,
                    "Disabled SAB",
                    "http://sab-disabled.invalid",
                    "disabled-secret",
                    0,
                    2,
                ),
            ),
        )
        db.execute(
            "INSERT INTO seen("
            "torrent_url,torrent_name,series_id,volume_num,protocol,client,"
            "download_id,download_client_id"
            ") VALUES('https://indexer.invalid/legacy-sab','Legacy SAB v01',"
            "1,1,'nzb','sabnzbd','LEGACY-NZO',NULL)"
        )
        db.execute(
            "INSERT INTO volumes("
            "series_id,volume_num,status,protocol,client,download_id,"
            "download_client_id"
            ") VALUES(1,1,'grabbed','nzb','sabnzbd','LEGACY-NZO',NULL)"
        )

    class _NetworkMustNotStart:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("ambiguous ownerless SAB row was polled")

    async def _no_suwayomi() -> None:
        return None

    monkeypatch.setattr(
        import_discovery.httpx,
        "AsyncClient",
        _NetworkMustNotStart,
    )
    monkeypatch.setattr(suwayomi_router, "check_suwayomi_jobs", _no_suwayomi)
    monkeypatch.setattr(
        import_discovery,
        "get_cfg",
        lambda key, default="": {
            "blocklist_ttl_days": "0",
            "failed_download_handling": "0",
        }.get(key, default),
    )

    asyncio.run(import_discovery._check_download_status_impl())

    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT status,download_id,download_client_id FROM volumes"
            " WHERE series_id=1"
        ).fetchone() == ("grabbed", "LEGACY-NZO", None)
        assert db.execute(
            "SELECT download_id,download_client_id FROM seen"
            " WHERE series_id=1"
        ).fetchone() == ("LEGACY-NZO", None)
        assert db.execute("SELECT COUNT(*) FROM import_queue").fetchone() == (0,)
        event = db.execute(
            "SELECT message FROM events"
            " WHERE event_type='configuration_error' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert event is not None
    assert "ownerless sabnzbd" in event[0]
    assert "2 possible owner(s)" in event[0]
    assert "1 enabled" in event[0]
    assert "ambiguous" in event[0]


def test_active_ownerless_import_protects_concrete_maintenance_rows(
    ownership_env: dict[str, Path],
) -> None:
    """NULL-owner active work fences matching concrete volume and seen rows."""
    import main
    from routers.system import _cleanup_stale_seen_rows, _reset_stuck_grabs

    db_path = ownership_env["db_path"]
    with sqlite3.connect(db_path) as db:
        _seed_series(db, 1, "Seen Fence")
        _seed_series(db, 2, "Volume Fence")
        db.execute(
            "INSERT INTO seen("
            "torrent_url,torrent_name,series_id,volume_num,grabbed_at,"
            "protocol,client,download_id,download_client_id"
            ") VALUES('magnet:seen-fence','Seen Fence v01',1,1,"
            "datetime('now','-100 days'),'torrent','qbittorrent',"
            "'SEEN-FENCE',501)"
        )
        db.execute(
            "INSERT INTO volumes("
            "series_id,volume_num,status,grabbed_at,protocol,client,"
            "download_id,download_client_id"
            ") VALUES(2,1,'grabbed',datetime('now','-3 days'),"
            "'torrent','qbittorrent','VOLUME-FENCE',502)"
        )
        db.executemany(
            "INSERT INTO import_queue("
            "series_id,download_id,download_client_id,download_protocol,status"
            ") VALUES(?,?,NULL,'torrent','pending')",
            (
                (1, "seen-fence"),
                (2, "volume-fence"),
            ),
        )

    with main.get_db() as db:
        assert _cleanup_stale_seen_rows(db) == 0
    with main.get_db() as db:
        assert _reset_stuck_grabs(db) == 0

    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT download_id,download_client_id FROM seen"
            " WHERE torrent_url='magnet:seen-fence'"
        ).fetchone() == ("SEEN-FENCE", 501)
        assert db.execute(
            "SELECT status,download_id,download_client_id FROM volumes"
            " WHERE series_id=2"
        ).fetchone() == ("grabbed", "VOLUME-FENCE", 502)


@pytest.mark.parametrize(
    ("client_type", "protocol", "queue_download_id", "stub_download_id"),
    (
        ("qbittorrent", "torrent", "ABCDEF", "abcdef"),
        ("sabnzbd", "nzb", "NZO-Collision", "NZO-Collision"),
    ),
)
def test_legacy_chapter_stub_promotion_requires_exact_client_owner(
    ownership_env: dict[str, Path],
    client_type: str,
    protocol: str,
    queue_download_id: str,
    stub_download_id: str,
) -> None:
    """A downloader-local collision cannot promote an unresolved file."""
    import main
    from import_plan import _plan_import

    source = _archive(
        ownership_env["downloads"] / client_type / "Unmapped release.cbz"
    )
    with sqlite3.connect(ownership_env["db_path"]) as db:
        _seed_series(db, 1, "Stub Ownership")
        db.executemany(
            "INSERT INTO download_clients("
            "id,name,type,host,password,enabled,priority"
            ") VALUES(?,?,?,?,?,1,?)",
            (
                (
                    701,
                    "Selected owner",
                    client_type,
                    "http://selected.invalid",
                    "secret",
                    1,
                ),
                (
                    702,
                    "Colliding owner",
                    client_type,
                    "http://collision.invalid",
                    "secret",
                    2,
                ),
            ),
        )
        db.execute(
            "INSERT INTO volumes("
            "series_id,status,pack_type,download_id,download_client_id,"
            "protocol,client"
            ") VALUES(1,'grabbed','chapter',?,702,?,?)",
            (stub_download_id, protocol, client_type),
        )
        queue_id = db.execute(
            "INSERT INTO import_queue("
            "series_id,download_id,download_client_id,torrent_name,torrent_url,"
            "src_dir,status"
            ") VALUES(1,?,701,'Unmapped release','https://source.invalid',"
            "?, 'pending')",
            (queue_download_id, str(source.parent)),
        ).lastrowid
        assert queue_id is not None
        file_id = db.execute(
            "INSERT INTO import_queue_files("
            "queue_id,filename,src_path,file_type,status"
            ") VALUES(?,'Unmapped release.cbz',?,'volume','pending')",
            (queue_id, str(source)),
        ).lastrowid
        assert file_id is not None

    with main.get_db() as db:
        assert main.claim_import_queue_row(db, int(queue_id), "plan-owner")
        plan = _plan_import(
            db,
            int(queue_id),
            "plan-owner",
            {},
            {},
            set(),
            "copy",
            lease_seconds=300,
        )

    assert plan is not None
    assert plan.files[0].plan_status == "needs_review"
    assert plan.files[0].is_legacy_chapter_stub is False
    with sqlite3.connect(ownership_env["db_path"]) as db:
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (file_id,),
        ).fetchone() == ("needs_review",)


@pytest.mark.parametrize("stub_owner", (None, 711), ids=("ownerless", "owned"))
def test_legacy_ownerless_queue_does_not_claim_chapter_stub(
    ownership_env: dict[str, Path],
    stub_owner: int | None,
) -> None:
    """NULL ownership remains unresolved instead of guessing a configured owner."""
    import main
    from import_plan import _plan_import

    source = _archive(
        ownership_env["downloads"] / "legacy" / "Ownerless unmapped.cbz"
    )
    with sqlite3.connect(ownership_env["db_path"]) as db:
        _seed_series(db, 1, "Legacy Stub Ownership")
        db.execute(
            "INSERT INTO download_clients("
            "id,name,type,host,password,enabled,priority"
            ") VALUES(711,'Configured qBit','qbittorrent',"
            "'http://qbit.invalid','secret',1,1)"
        )
        db.execute(
            "INSERT INTO volumes("
            "series_id,status,pack_type,download_id,download_client_id,"
            "protocol,client"
            ") VALUES(1,'grabbed','chapter','LEGACY-ID',?,"
            "'torrent','qbittorrent')",
            (stub_owner,),
        )
        queue_id = db.execute(
            "INSERT INTO import_queue("
            "series_id,download_id,download_client_id,torrent_name,torrent_url,"
            "src_dir,status"
            ") VALUES(1,'LEGACY-ID',NULL,'Ownerless unmapped',"
            "'magnet:legacy',?,'pending')",
            (str(source.parent),),
        ).lastrowid
        assert queue_id is not None
        db.execute(
            "INSERT INTO import_queue_files("
            "queue_id,filename,src_path,file_type,status"
            ") VALUES(?,'Ownerless unmapped.cbz',?,'volume','pending')",
            (queue_id, str(source)),
        )

    with main.get_db() as db:
        assert main.claim_import_queue_row(db, int(queue_id), "legacy-plan-owner")
        plan = _plan_import(
            db,
            int(queue_id),
            "legacy-plan-owner",
            {},
            {},
            set(),
            "copy",
            lease_seconds=300,
        )

    assert plan is not None
    assert plan.files[0].plan_status == "needs_review"
    assert plan.files[0].is_legacy_chapter_stub is False


@pytest.mark.parametrize(
    ("client_type", "protocol", "target_id", "queued_id"),
    (
        ("qbittorrent", "torrent", "ABCDEF", "abcdef"),
        ("sabnzbd", "nzb", "NZO-Collision", "NZO-Collision"),
    ),
)
def test_orphan_cleanup_transitions_only_the_exact_client_owner(
    ownership_env: dict[str, Path],
    client_type: str,
    protocol: Literal["torrent", "nzb"],
    target_id: str,
    queued_id: str,
) -> None:
    """An active row on another client cannot hijack or block exact cleanup."""
    import import_discovery
    import main

    with sqlite3.connect(ownership_env["db_path"]) as db:
        _seed_series(db, 1, "Orphan Ownership")
        db.executemany(
            "INSERT INTO download_clients("
            "id,name,type,host,password,enabled,priority"
            ") VALUES(?,?,?,?,?,1,?)",
            (
                (
                    801,
                    "Orphan owner",
                    client_type,
                    "http://orphan.invalid",
                    "secret",
                    1,
                ),
                (
                    802,
                    "Collision owner",
                    client_type,
                    "http://collision.invalid",
                    "secret",
                    2,
                ),
            ),
        )
        failed_id = db.execute(
            "INSERT INTO import_queue("
            "series_id,download_id,download_client_id,torrent_name,status"
            ") VALUES(1,?,801,'Failed exact owner','failed')",
            (queued_id,),
        ).lastrowid
        active_id = db.execute(
            "INSERT INTO import_queue("
            "series_id,download_id,download_client_id,torrent_name,status"
            ") VALUES(1,?,802,'Active collision','pending')",
            (queued_id,),
        ).lastrowid
        assert failed_id is not None and active_id is not None
        failed_child = db.execute(
            "INSERT INTO import_queue_files(queue_id,filename,status)"
            " VALUES(?,'failed.cbz','failed')",
            (failed_id,),
        ).lastrowid
        assert failed_child is not None

    with main.get_db() as db:
        cleanup_allowed, transitioned = import_discovery._reserve_orphan_cleanup(
            db,
            target_id,
            download_client_id=801,
            protocol=protocol,
        )

    assert cleanup_allowed
    assert transitioned == [failed_id]
    with sqlite3.connect(ownership_env["db_path"]) as db:
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (failed_id,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status FROM import_queue_files WHERE id=?",
            (failed_child,),
        ).fetchone() == ("skipped",)
        assert db.execute(
            "SELECT status FROM import_queue WHERE id=?",
            (active_id,),
        ).fetchone() == ("pending",)


def test_qbit_deduplication_accepts_sqlite_rows(
    ownership_env: dict[str, Path],
) -> None:
    """Discovery never calls dict-only methods on sqlite3.Row."""
    import import_discovery

    with sqlite3.connect(ownership_env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT 1 AS series_id, 'ABCDEF' AS download_id,"
            " 801 AS download_client_id, 'Owned release' AS torrent_name"
        ).fetchone()
    assert row is not None
    torrent = {"hash": "abcdef", "name": "Owned release"}

    matched = import_discovery._deduplicate_qbit_matches(
        [row],
        {"abcdef": torrent},
        {},
    )

    assert matched == [(row, torrent, "abcdef")]


@pytest.mark.parametrize(
    ("client_type", "protocol", "download_id"),
    (
        ("qbittorrent", "torrent", "ABCDEF"),
        ("sabnzbd", "nzb", "NZO-COLLISION"),
    ),
)
@pytest.mark.parametrize(
    "import_kind",
    ("volume", "volume_range", "chapter", "special"),
)
def test_phase3_metadata_uses_queue_owner_across_every_import_branch(
    ownership_env: dict[str, Path],
    client_type: str,
    protocol: str,
    download_id: str,
    import_kind: str,
) -> None:
    import import_execute
    import import_queue
    import main

    db_path = ownership_env["db_path"]
    selected_owner = 301
    collision_owner = 302
    filenames = {
        "volume": "Owned Series v01.cbz",
        "volume_range": "Owned Series v01-v03.cbz",
        "chapter": "Owned Series c001.cbz",
        "special": "Owned Series Bonus.cbz",
    }
    release_names = {
        "volume": "Owned Series v01",
        "volume_range": "Owned Series v01-v03",
        "chapter": "Owned Series c001",
        "special": "Owned Series Special Bonus",
    }
    source_dir = ownership_env["downloads"] / f"{protocol}-{import_kind}"
    _archive(source_dir / filenames[import_kind])
    with sqlite3.connect(db_path) as db:
        _seed_series(db, 1, "Owned Series")
        db.executemany(
            "INSERT INTO download_clients(id,name,type,host,password,enabled,priority)"
            " VALUES(?,?,?,?,?,1,?)",
            (
                (
                    selected_owner,
                    "Selected owner",
                    client_type,
                    f"http://selected-{protocol}.invalid",
                    "selected-secret",
                    2,
                ),
                (
                    collision_owner,
                    "Collision owner",
                    client_type,
                    f"http://collision-{protocol}.invalid",
                    "collision-secret",
                    1,
                ),
            ),
        )
        db.executemany(
            "INSERT INTO seen("
            "torrent_url,torrent_name,series_id,volume_num,indexer,protocol,"
            "client,download_id,download_client_id,release_group,size_bytes"
            ") VALUES(?,?,1,1,?,?,?,?,?,?,?)",
            (
                (
                    f"https://collision.invalid/{protocol}/{import_kind}",
                    "Wrong collision metadata",
                    "wrong-indexer",
                    protocol,
                    client_type,
                    download_id,
                    collision_owner,
                    "wrong-group",
                    999,
                ),
                (
                    f"https://selected.invalid/{protocol}/{import_kind}",
                    "Selected metadata",
                    "selected-indexer",
                    protocol,
                    client_type,
                    (
                        download_id.lower()
                        if protocol == "torrent"
                        else download_id
                    ),
                    selected_owner,
                    "selected-group",
                    123,
                ),
            ),
        )

    with main.get_db() as db:
        queue_id, _ = import_queue._queue_import(
            db,
            1,
            download_id,
            release_names[import_kind],
            f"https://selected.invalid/{protocol}/{import_kind}",
            1.0 if import_kind == "volume" else None,
            str(source_dir),
            download_client_id=selected_owner,
            protocol=protocol,
        )
        assert queue_id is not None
        if import_kind == "special":
            db.execute(
                "UPDATE import_queue_files"
                " SET status='pending', proposed_import_kind='special',"
                " proposed_is_special=1, proposed_special_title='Bonus'"
                " WHERE queue_id=?",
                (queue_id,),
            )

    assert asyncio.run(import_execute._execute_import(queue_id))
    with sqlite3.connect(db_path) as db:
        if import_kind == "chapter":
            row = db.execute(
                "SELECT download_client_id,torrent_name,indexer,protocol,client,"
                "release_group,size_bytes FROM chapters"
                " WHERE series_id=1 AND chapter_num=1"
            ).fetchone()
        elif import_kind == "special":
            row = db.execute(
                "SELECT download_client_id,torrent_name,indexer,protocol,client,"
                "release_group,size_bytes FROM volumes"
                " WHERE series_id=1 AND is_special=1"
            ).fetchone()
        elif import_kind == "volume_range":
            row = db.execute(
                "SELECT download_client_id,torrent_name,indexer,protocol,client,"
                "release_group,size_bytes FROM volumes"
                " WHERE series_id=1 AND volume_num IS NULL"
                " AND vol_range_start=1 AND vol_range_end=3"
            ).fetchone()
        else:
            row = db.execute(
                "SELECT download_client_id,torrent_name,indexer,protocol,client,"
                "release_group,size_bytes FROM volumes"
                " WHERE series_id=1 AND volume_num=1"
            ).fetchone()
        history_owner = db.execute(
            "SELECT download_client_id FROM history"
            " WHERE series_id=1 AND event_type='imported'"
        ).fetchone()
    assert row == (
        selected_owner,
        "Selected metadata",
        "selected-indexer",
        protocol,
        client_type,
        "selected-group",
        123,
    )
    assert history_owner == (selected_owner,)


def test_queue_dedup_and_siblings_share_owner_protocol_identity(
    ownership_env: dict[str, Path],
) -> None:
    import import_queue
    import main
    from import_lease import has_import_sibling_that_may_use_download

    source_a = _archive(ownership_env["downloads"] / "a" / "Owned v01.cbz")
    source_b = _archive(ownership_env["downloads"] / "b" / "Owned v02.cbz")
    with sqlite3.connect(ownership_env["db_path"]) as db:
        _seed_series(db, 1, "Owned")
        db.executemany(
            "INSERT INTO download_clients(id,name,type,host,password,enabled,priority)"
            " VALUES(?,?,'qbittorrent',?,'p',1,?)",
            (
                (401, "qBit A", "http://qbit-a.invalid", 1),
                (402, "qBit B", "http://qbit-b.invalid", 2),
            ),
        )

    with main.get_db() as db:
        queue_a, _ = import_queue._queue_import(
            db,
            1,
            "ABCDEF",
            "Owned v01",
            "magnet:a",
            1.0,
            str(source_a),
            download_client_id=401,
            protocol="torrent",
        )
    assert queue_a is not None
    with main.get_db() as db:
        assert import_queue._queue_import(
            db,
            1,
            "abcdef",
            "Owned v01 duplicate",
            "magnet:a",
            1.0,
            str(source_a),
            download_client_id=401,
            protocol="torrent",
        ) == (queue_a, False)
        queue_b, _ = import_queue._queue_import(
            db,
            1,
            "abcdef",
            "Owned v02",
            "magnet:b",
            2.0,
            str(source_b),
            download_client_id=402,
            protocol="torrent",
        )
    assert queue_b is not None and queue_b != queue_a

    with main.get_db() as db:
        same_owner_id = db.execute(
            "INSERT INTO import_queue("
            "series_id,download_id,download_client_id,torrent_name,status"
            ") VALUES(1,'abcdef',401,'Same owner case variant','pending')"
        ).lastrowid
        assert same_owner_id is not None
        assert has_import_sibling_that_may_use_download(
            db,
            queue_id=queue_a,
            download_id="ABCDEF",
            download_client_id=401,
            series_id=1,
            protocol="torrent",
        )
        db.execute("DELETE FROM import_queue WHERE id=?", (same_owner_id,))
        assert not has_import_sibling_that_may_use_download(
            db,
            queue_id=queue_a,
            download_id="ABCDEF",
            download_client_id=401,
            series_id=1,
            protocol="torrent",
        )
        db.execute(
            "INSERT INTO import_queue("
            "series_id,download_id,download_client_id,torrent_name,status"
            ") VALUES(1,'abcdef',NULL,'Legacy','pending')"
        )
        assert has_import_sibling_that_may_use_download(
            db,
            queue_id=queue_a,
            download_id="ABCDEF",
            download_client_id=401,
            series_id=1,
            protocol="torrent",
        )


def test_sab_identity_is_exact_and_legacy_commit_owner_stays_null(
    ownership_env: dict[str, Path],
) -> None:
    import import_execute
    import import_queue
    import main
    from import_lease import has_import_sibling_that_may_use_download

    source = _archive(
        ownership_env["downloads"] / "legacy" / "Legacy Owned v01.cbz"
    )
    with sqlite3.connect(ownership_env["db_path"]) as db:
        _seed_series(db, 1, "Legacy Owned")
        db.execute(
            "INSERT INTO download_clients("
            "id,name,type,host,password,enabled,priority"
            ") VALUES(501,'SAB','sabnzbd','http://sab.invalid','key',1,1)"
        )
        db.executemany(
            "INSERT INTO seen("
            "torrent_url,torrent_name,series_id,volume_num,indexer,protocol,"
            "client,download_id,download_client_id"
            ") VALUES(?,?,1,1,?,'nzb','sabnzbd',?,?)",
            (
                (
                    "https://legacy.invalid/owned",
                    "Ownerless metadata",
                    "legacy-indexer",
                    "NZO-Legacy",
                    None,
                ),
                (
                    "https://legacy.invalid/collision",
                    "Collision metadata",
                    "collision-indexer",
                    "NZO-Legacy",
                    501,
                ),
            ),
        )

    with main.get_db() as db:
        queue_id, _ = import_queue._queue_import(
            db,
            1,
            "NZO-Legacy",
            "Legacy Owned v01",
            "https://legacy.invalid/owned",
            1.0,
            str(source),
            download_client_id=None,
            protocol="nzb",
        )
        assert queue_id is not None
        case_variant_id = db.execute(
            "INSERT INTO import_queue("
            "series_id,download_id,download_client_id,torrent_name,status"
            ") VALUES(1,'nzo-legacy',501,'Case variant','pending')"
        ).lastrowid
        assert case_variant_id is not None
        assert not has_import_sibling_that_may_use_download(
            db,
            queue_id=int(case_variant_id),
            download_id="nzo-legacy",
            download_client_id=501,
            series_id=1,
            protocol="nzb",
        )
        db.execute("DELETE FROM import_queue WHERE id=?", (case_variant_id,))

    assert asyncio.run(import_execute._execute_import(queue_id))
    with sqlite3.connect(ownership_env["db_path"]) as db:
        row = db.execute(
            "SELECT download_client_id,torrent_name,indexer"
            " FROM volumes WHERE series_id=1 AND volume_num=1"
        ).fetchone()
    assert row == (None, "Ownerless metadata", "legacy-indexer")
