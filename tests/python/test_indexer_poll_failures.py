"""Regression coverage for indexer polling selection and failure events."""

from __future__ import annotations

import asyncio
import sqlite3
import sys

import httpx
import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401, E402


@pytest.fixture
def indexer_env(tmp_path, monkeypatch):
    import main
    import security
    import shared

    db_path = str(tmp_path / "indexer-poll.db")
    key_dir = str(tmp_path / "keys")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(shared, "DB_PATH", db_path)
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    security.load_or_create_secret_cipher(key_dir)
    main.init_db()
    main.load_config()
    return db_path


def _seed_parent_and_child(
    db_path: str,
    *,
    child_enabled: int = 1,
    child_use_rss: int | None = 1,
    child_use_auto_search: int | None = 1,
    child_use_interactive_search: int | None = 1,
) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO indexers("
            " id, name, type, url, api_key, priority, enabled,"
            " use_rss, use_auto_search, use_interactive_search"
            ") VALUES(16, 'Prowlarr', 'prowlarr', 'http://prowlarr.test',"
            " 'key', 20, 1, 1, 1, 1)"
        )
        db.execute(
            "INSERT INTO indexers("
            " id, name, type, url, api_key, priority, enabled,"
            " parent_prowlarr_id, prowlarr_indexer_id,"
            " use_rss, use_auto_search, use_interactive_search"
            ") VALUES(19, 'GazelleGames', 'torznab',"
            " 'http://prowlarr.test/23', 'key', 10, ?, 16, 23, ?, ?, ?)",
            (
                child_enabled,
                child_use_rss,
                child_use_auto_search,
                child_use_interactive_search,
            ),
        )


def _parent_indexer() -> dict:
    return {
        "id": 16,
        "name": "Prowlarr",
        "type": "prowlarr",
        "url": "http://prowlarr.test",
        "api_key": "not-logged",
        "categories": "[7000]",
    }


def test_rss_assigns_imported_child_id_without_suppressing_parent(
    indexer_env, monkeypatch
):
    from routers import indexers
    from shared import get_db

    _seed_parent_and_child(indexer_env)
    polled: list[tuple[int, frozenset[int]]] = []

    async def fake_fetch(
        idx: dict, *, excluded_prowlarr_ids: frozenset[int] = frozenset()
    ) -> list[dict]:
        polled.append((idx["id"], excluded_prowlarr_ids))
        return []

    monkeypatch.setattr(indexers, "_fetch_rss_for_indexer", fake_fetch)
    with get_db() as db:
        asyncio.run(indexers.fetch_all_rss(db))

    assert polled == [(19, frozenset()), (16, frozenset({23}))]


def test_rss_keeps_parent_when_no_child_participates_in_rss(
    indexer_env, monkeypatch
):
    from routers import indexers
    from shared import get_db

    _seed_parent_and_child(indexer_env, child_use_rss=0)
    polled: list[tuple[int, frozenset[int]]] = []

    async def fake_fetch(
        idx: dict, *, excluded_prowlarr_ids: frozenset[int] = frozenset()
    ) -> list[dict]:
        polled.append((idx["id"], excluded_prowlarr_ids))
        return []

    monkeypatch.setattr(indexers, "_fetch_rss_for_indexer", fake_fetch)
    with get_db() as db:
        asyncio.run(indexers.fetch_all_rss(db))

    assert polled == [(16, frozenset())]


def test_disabled_imported_child_falls_back_to_enabled_parent(
    indexer_env, monkeypatch
):
    from routers import indexers
    from shared import get_db

    _seed_parent_and_child(indexer_env, child_enabled=0)
    polled: list[tuple[int, frozenset[int]]] = []

    async def fake_fetch(
        idx: dict, *, excluded_prowlarr_ids: frozenset[int] = frozenset()
    ) -> list[dict]:
        polled.append((idx["id"], excluded_prowlarr_ids))
        return []

    monkeypatch.setattr(indexers, "_fetch_rss_for_indexer", fake_fetch)
    with get_db() as db:
        asyncio.run(indexers.fetch_all_rss(db))

    assert polled == [(16, frozenset())]


@pytest.mark.parametrize("purpose", ["auto", "interactive"])
def test_search_assigns_imported_child_id_without_suppressing_parent(
    indexer_env, monkeypatch, purpose
):
    from routers import indexers
    from shared import get_db

    _seed_parent_and_child(indexer_env)
    searched: list[tuple[int, frozenset[int]]] = []

    async def fake_search(
        idx: dict,
        query: str,
        *,
        excluded_prowlarr_ids: frozenset[int] = frozenset(),
    ) -> list[dict]:
        assert query == "Berserk"
        searched.append((idx["id"], excluded_prowlarr_ids))
        return []

    monkeypatch.setattr(indexers, "_search_indexer", fake_search)
    with get_db() as db:
        asyncio.run(indexers.search_all_indexers(db, "Berserk", purpose=purpose))

    assert searched == [(19, frozenset()), (16, frozenset({23}))]


def test_tag_ineligible_child_does_not_suppress_eligible_parent(
    indexer_env, monkeypatch
):
    from routers import indexers
    from shared import get_db

    _seed_parent_and_child(indexer_env)
    with sqlite3.connect(indexer_env) as db:
        db.execute(
            "INSERT INTO series("
            " id, title, search_pattern, edition_type, enabled, monitored,"
            " monitor_mode"
            ") VALUES(100, 'Berserk', 'Berserk', 'standard', 1, 1, 'all')"
        )
        db.execute(
            "INSERT INTO indexer_tags(indexer_id, tag) VALUES(19, 'private')"
        )
        db.execute(
            "INSERT INTO series_tags(series_id, tag) VALUES(100, 'public')"
        )

    searched: list[tuple[int, frozenset[int]]] = []

    async def fake_search(
        idx: dict,
        query: str,
        *,
        excluded_prowlarr_ids: frozenset[int] = frozenset(),
    ) -> list[dict]:
        searched.append((idx["id"], excluded_prowlarr_ids))
        return []

    monkeypatch.setattr(indexers, "_search_indexer", fake_search)
    with get_db() as db:
        asyncio.run(
            indexers.search_all_indexers(
                db, "Berserk", purpose="auto", series_id=100
            )
        )

    assert searched == [(16, frozenset())]


@pytest.mark.parametrize(
    ("operation", "query"),
    [("rss", ""), ("search", "Berserk")],
)
def test_parent_fanout_skips_owned_child_but_queries_unimported_source(
    indexer_env, monkeypatch, operation, query
):
    from routers import indexers

    _seed_parent_and_child(indexer_env)
    requested: list[tuple[int, str]] = []

    async def fake_sub_indexers(*args, **kwargs):
        return [
            (23, "GazelleGames", "torrent"),
            (24, "UnimportedTracker", "torrent"),
        ]

    async def fake_fetch(
        url, key, indexer_id, name, protocol, cats, *, query
    ) -> list[dict]:
        requested.append((indexer_id, query))
        return []

    monkeypatch.setattr(indexers, "_get_prowlarr_indexers", fake_sub_indexers)
    monkeypatch.setattr(indexers, "_fetch_prowlarr_results", fake_fetch)
    monkeypatch.setattr(indexers, "validate_outbound_url", lambda *args, **kwargs: None)

    if operation == "rss":
        asyncio.run(
            indexers._fetch_rss_for_indexer(
                _parent_indexer(), excluded_prowlarr_ids=frozenset({23})
            )
        )
    else:
        asyncio.run(
            indexers._search_indexer(
                _parent_indexer(),
                query,
                excluded_prowlarr_ids=frozenset({23}),
            )
        )

    assert requested == [(24, query)]


@pytest.mark.parametrize(
    ("operation", "exception", "expected_severity", "expected_detail"),
    [
        ("rss", httpx.ReadTimeout(""), "warning", "ReadTimeout: request timed out"),
        (
            "search",
            httpx.ConnectError(""),
            "warning",
            "ConnectError: connection failed",
        ),
        (
            "rss",
            httpx.DecodingError(
                "decode failed at https://tracker.test/api?apikey=SECRET",
                request=httpx.Request(
                    "GET", "https://tracker.test/api?apikey=SECRET"
                ),
            ),
            "warning",
            "DecodingError: response decoding failed",
        ),
        (
            "search",
            httpx.TooManyRedirects(
                "redirected through https://tracker.test/api?apikey=SECRET",
                request=httpx.Request(
                    "GET", "https://tracker.test/api?apikey=SECRET"
                ),
            ),
            "warning",
            "TooManyRedirects: too many redirects",
        ),
        (
            "rss",
            httpx.RequestError(
                "request failed at https://tracker.test/api?apikey=SECRET",
                request=httpx.Request(
                    "GET", "https://tracker.test/api?apikey=SECRET"
                ),
            ),
            "warning",
            "RequestError: HTTP request failed",
        ),
        ("rss", RuntimeError(""), "error", "RuntimeError: no detail provided"),
        ("search", ValueError("bad parser state"), "error", "ValueError: bad parser state"),
    ],
)
def test_rss_and_search_exception_events_are_actionable(
    indexer_env,
    monkeypatch,
    operation,
    exception,
    expected_severity,
    expected_detail,
):
    from routers import indexers

    _seed_parent_and_child(indexer_env)

    class RaisingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            raise exception

    events: list[tuple[str, str]] = []
    monkeypatch.setattr(indexers.httpx, "AsyncClient", RaisingClient)
    monkeypatch.setattr(indexers, "validate_outbound_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        indexers,
        "log_event",
        lambda event_type, message, *args, **kwargs: events.append(
            (event_type, message)
        ),
    )
    idx = {
        "id": 19,
        "name": "GazelleGames",
        "type": "torznab",
        "url": "http://prowlarr.test/23",
        "api_key": "not-logged",
        "categories": "[7000]",
    }

    if operation == "rss":
        asyncio.run(indexers._fetch_rss_for_indexer(idx))
    else:
        asyncio.run(indexers._search_indexer(idx, "Berserk"))

    assert len(events) == 1
    severity, message = events[0]
    assert severity == expected_severity
    assert expected_detail in message
    assert not message.endswith(": ")
    assert "not-logged" not in message
    assert "SECRET" not in message
    assert "tracker.test" not in message


@pytest.mark.parametrize("operation", ["rss", "search"])
def test_repeated_malformed_xml_increments_backoff_without_success_reset(
    indexer_env, monkeypatch, operation
):
    from routers import indexers

    _seed_parent_and_child(indexer_env)

    class MalformedResponseClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return httpx.Response(200, text="<rss><channel>")

    events: list[tuple[str, str]] = []
    monkeypatch.setattr(indexers.httpx, "AsyncClient", MalformedResponseClient)
    monkeypatch.setattr(indexers, "validate_outbound_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        indexers, "_indexer_is_backed_off", lambda indexer_id: (False, 0.0)
    )
    monkeypatch.setattr(indexers.time, "time", lambda: 1000.0)
    monkeypatch.setattr(
        indexers,
        "log_event",
        lambda event_type, message, *args, **kwargs: events.append(
            (event_type, message)
        ),
    )
    idx = {
        "id": 19,
        "name": "GazelleGames",
        "type": "torznab",
        "url": "http://prowlarr.test/23",
        "api_key": "not-logged",
        "categories": "[7000]",
    }

    for _ in range(2):
        if operation == "rss":
            assert asyncio.run(indexers._fetch_rss_for_indexer(idx)) == []
        else:
            assert asyncio.run(indexers._search_indexer(idx, "Berserk")) == []

    assert len(events) == 2
    assert all(event_type == "error" for event_type, _ in events)
    assert all("ParseError:" in message for _, message in events)
    with sqlite3.connect(indexer_env) as db:
        row = db.execute(
            "SELECT consecutive_failures, retry_after, last_status"
            " FROM indexer_backoff WHERE indexer_id=19"
        ).fetchone()
    assert row == (2, 1120.0, None)


def test_parent_fanout_logs_failed_subindexer_once_and_sets_backoff(
    indexer_env, monkeypatch
):
    from routers import indexers

    _seed_parent_and_child(indexer_env)
    events: list[tuple[str, str]] = []

    async def fake_sub_indexers(*args, **kwargs):
        return [(23, "GazelleGames", "torrent")]

    async def timed_out(*args, **kwargs):
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(indexers, "_get_prowlarr_indexers", fake_sub_indexers)
    monkeypatch.setattr(indexers, "_fetch_prowlarr_results", timed_out)
    monkeypatch.setattr(indexers, "validate_outbound_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        indexers,
        "log_event",
        lambda event_type, message, *args, **kwargs: events.append(
            (event_type, message)
        ),
    )

    assert asyncio.run(indexers._fetch_rss_for_indexer(_parent_indexer())) == []

    assert events == [
        (
            "warning",
            "[Prowlarr:GazelleGames] RSS request failed: ReadTimeout: "
            "request timed out; check Prowlarr and upstream tracker availability",
        )
    ]
    with sqlite3.connect(indexer_env) as db:
        row = db.execute(
            "SELECT consecutive_failures, last_reason"
            " FROM indexer_backoff WHERE indexer_id=16"
        ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert "ReadTimeout: request timed out" in row[1]


@pytest.mark.parametrize("failure_site", ["parent-list", "sub-indexer"])
def test_parent_http_status_failure_preserves_429_retry_after(
    indexer_env, monkeypatch, failure_site
):
    from routers import indexers

    _seed_parent_and_child(indexer_env)
    request = httpx.Request(
        "GET", "https://prowlarr.test/api/v1/indexer?apikey=SECRET"
    )
    response = httpx.Response(
        429, headers={"Retry-After": "120"}, request=request
    )
    failure = httpx.HTTPStatusError(
        "429 for secret-bearing request", request=request, response=response
    )
    events: list[tuple[str, str]] = []

    async def list_or_fail(*args, **kwargs):
        if failure_site == "parent-list":
            raise failure
        return [(24, "RateLimitedTracker", "torrent")]

    async def fetch_or_fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(indexers, "_get_prowlarr_indexers", list_or_fail)
    monkeypatch.setattr(indexers, "_fetch_prowlarr_results", fetch_or_fail)
    monkeypatch.setattr(indexers, "validate_outbound_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        indexers,
        "log_event",
        lambda event_type, message, *args, **kwargs: events.append(
            (event_type, message)
        ),
    )

    before = indexers.time.time()
    assert asyncio.run(indexers._fetch_rss_for_indexer(_parent_indexer())) == []

    with sqlite3.connect(indexer_env) as db:
        row = db.execute(
            "SELECT retry_after, last_status, last_reason"
            " FROM indexer_backoff WHERE indexer_id=16"
        ).fetchone()
    assert row is not None
    assert 119 <= row[0] - before <= 121
    assert row[1] == 429
    assert "HTTPStatusError: upstream returned HTTP 429" in row[2]
    assert len(events) == 1
    assert events[0][0] == "warning"
    assert "SECRET" not in events[0][1]
    assert "prowlarr.test" not in events[0][1]


def test_parent_fanout_honors_longest_retry_after_across_failures(
    indexer_env, monkeypatch
):
    from routers import indexers

    _seed_parent_and_child(indexer_env)

    async def fake_sub_indexers(*args, **kwargs):
        return [
            (24, "ShortBackoff", "torrent"),
            (25, "LongBackoff", "torrent"),
        ]

    async def rate_limited(
        url, key, indexer_id, name, protocol, cats, *, query
    ):
        request = httpx.Request(
            "GET", f"https://prowlarr.test/indexer/{indexer_id}"
        )
        retry_after = "30" if indexer_id == 24 else "300"
        response = httpx.Response(
            429, headers={"Retry-After": retry_after}, request=request
        )
        raise httpx.HTTPStatusError(
            "rate limited", request=request, response=response
        )

    monkeypatch.setattr(indexers, "_get_prowlarr_indexers", fake_sub_indexers)
    monkeypatch.setattr(indexers, "_fetch_prowlarr_results", rate_limited)
    monkeypatch.setattr(indexers, "validate_outbound_url", lambda *args, **kwargs: None)

    before = indexers.time.time()
    assert asyncio.run(indexers._fetch_rss_for_indexer(_parent_indexer())) == []

    with sqlite3.connect(indexer_env) as db:
        row = db.execute(
            "SELECT retry_after, consecutive_failures, last_status"
            " FROM indexer_backoff WHERE indexer_id=16"
        ).fetchone()
    assert row is not None
    assert 299 <= row[0] - before <= 301
    assert row[1:] == (1, 429)


@pytest.mark.parametrize("operation", ["rss", "search"])
def test_parent_fanout_propagates_cancelled_error(
    indexer_env, monkeypatch, operation
):
    from routers import indexers

    _seed_parent_and_child(indexer_env)

    async def fake_sub_indexers(*args, **kwargs):
        return [(24, "SlowTracker", "torrent")]

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(indexers, "_get_prowlarr_indexers", fake_sub_indexers)
    monkeypatch.setattr(indexers, "_fetch_prowlarr_results", cancelled)
    monkeypatch.setattr(indexers, "validate_outbound_url", lambda *args, **kwargs: None)

    with pytest.raises(asyncio.CancelledError):
        if operation == "rss":
            asyncio.run(indexers._fetch_rss_for_indexer(_parent_indexer()))
        else:
            asyncio.run(indexers._search_indexer(_parent_indexer(), "Berserk"))


def test_transient_http_statuses_warn_but_forbidden_remains_error():
    from routers.indexers import (
        _response_failure_reason,
        _response_failure_severity,
    )

    assert _response_failure_severity(429) == "warning"
    assert _response_failure_severity(502) == "warning"
    assert _response_failure_severity(403) == "error"
    assert _response_failure_reason(httpx.Response(502)) == "server error (502)"
    assert _response_failure_reason(httpx.Response(401)) == (
        "unexpected HTTP status (401)"
    )
    assert _response_failure_reason(httpx.Response(200)) is None
