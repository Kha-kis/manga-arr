"""Map coverage is not evidence of an upstream outage."""

import asyncio
import json
import sqlite3
from unittest.mock import patch

import httpx
import pytest

from test_metadata_lifecycle import db_path, _seed_series  # noqa: F401


USABLE = {"1": 1, "2": 2}
MDX_USABLE = {
    "volumes": {
        "1": {"chapters": {"1": {}}},
        "2": {"chapters": {"2": {}}},
    }
}
KITSU_SEARCH = {"data": [{"id": "7", "attributes": {"canonicalTitle": "Exact Series"}}]}


def _response(request, kind, provider):
    if kind == "timeout":
        raise httpx.ReadTimeout("private transport context", request=request)
    if kind == "transport_error":
        raise httpx.ConnectError("private transport context", request=request)
    if kind in {"429", "503"}:
        return httpx.Response(int(kind), json={"error": "unavailable"})
    if kind == "invalid_json":
        return httpx.Response(200, content=b"not json")
    if kind == "malformed_payload":
        return httpx.Response(
            200, json={"volumes": []} if provider == "mangadex" else {"data": {}}
        )
    if provider == "mangadex":
        payload = MDX_USABLE if kind == "usable" else {"volumes": {}}
        if kind == "sparse":
            payload = {"volumes": {"1": {"chapters": {"1": {}}}}}
    else:
        payload = {"data": [], "links": {}}
        if kind in {"usable", "sparse"}:
            payload["data"] = [
                {"attributes": {"number": ch, "volumeNumber": vol}}
                for ch, vol in (USABLE if kind == "usable" else {"1": 1}).items()
            ]
    return httpx.Response(200, json=payload)


def _client_factory(mdx_kind, kitsu_kind, *, kitsu_stage="chapters"):
    original = httpx.AsyncClient

    def handler(request):
        if request.url.host == "api.mangadex.org":
            assert request.url.path.endswith("/aggregate")
            return _response(request, mdx_kind, "mangadex")
        assert request.url.host == "kitsu.io"
        if request.url.path.endswith("/manga") and kitsu_stage == "chapters":
            return httpx.Response(200, json=KITSU_SEARCH)
        return _response(request, kitsu_kind, "kitsu")

    def factory(*args, **kwargs):
        return original(*args, **kwargs, transport=httpx.MockTransport(handler))

    return factory


@pytest.mark.parametrize(
    ("mdx_kind", "kitsu_kind", "failure", "reason"),
    [
        ("empty", "empty", False, "empty"),
        ("sparse", "empty", False, "insufficient_coverage"),
        ("empty", "sparse", False, "insufficient_coverage"),
        *[
            (kind, "empty", True, expected)
            for kind, expected in [
                ("429", "http_error"),
                ("503", "http_error"),
                ("timeout", "timeout"),
                ("transport_error", "transport_error"),
                ("invalid_json", "invalid_json"),
                ("malformed_payload", "malformed_payload"),
            ]
        ],
        ("empty", "503", True, "http_error"),
        ("503", "503", True, "http_error"),
    ],
)
@pytest.mark.parametrize("apply_changes", [False, True])
def test_refresh_distinguishes_coverage_from_provider_failures(
    db_path, mdx_kind, kitsu_kind, failure, reason, apply_changes
):
    import metadata_enrichment as enrichment
    import metadata_state as state

    _seed_series(
        db_path, mangadex_id="mdx-1", mal_id=456, mu_id="789", total_chapters=20
    )
    # A previous outage must be cleared by a credible successful empty response.
    state.mark_source_failure(7, state.SOURCE_CHAPTER_MAP, "previous outage")
    with (
        patch.object(httpx, "AsyncClient", _client_factory(mdx_kind, kitsu_kind)),
        patch("rescan._series_library_dir", return_value=None),
    ):
        assert (
            asyncio.run(enrichment.refresh_mangadex_map(7, apply_changes=apply_changes))
            is False
        )
    source = state.get_source_states(7)[0]
    assert source["failure_count"] == (2 if failure else 0)
    assert bool(source["next_retry_at"]) is failure
    assert source["status"] == ("failed" if failure else "degraded")
    assert bool(source["last_success_at"]) is not failure
    assert source["details"]["outcome"] == (
        "provider_failure" if failure else "insufficient_coverage"
    )
    providers = source["details"]["providers"]
    target = "kitsu" if mdx_kind == "empty" and kitsu_kind != "empty" else "mangadex"
    assert providers[target]["reason"] == reason
    assert "private transport context" not in source["error"]
    health = state.build_catalog_metadata_health(7)
    assert ("provider_failures" in health["issues"]) is failure
    assert (state.SOURCE_CHAPTER_MAP in health["failed_sources"]) is failure
    assert "missing_chapter_map" in health["issues"]


@pytest.mark.parametrize("fallback", ["kitsu", "cbz"])
def test_usable_fallback_wins_over_provider_failure(db_path, fallback):
    import metadata_enrichment as enrichment
    import metadata_state as state

    _seed_series(db_path, mangadex_id="mdx-1", mal_id=456, mu_id="789")
    with (
        patch.object(
            httpx,
            "AsyncClient",
            _client_factory("503", "usable" if fallback == "kitsu" else "empty"),
        ),
        patch("rescan._series_library_dir", return_value="/unused"),
        patch.object(enrichment, "_extract_map_from_cbzs", return_value=USABLE),
    ):
        assert asyncio.run(enrichment.refresh_mangadex_map(7)) is True
    source = state.get_source_states(7)[0]
    assert source["status"] == "healthy"
    assert source["failure_count"] == 0
    assert source["next_retry_at"] is None
    assert source["details"]["source"] == fallback
    assert source["details"]["providers"]["mangadex"]["reason"] == "http_error"


@pytest.mark.parametrize("provider", ["mangadex", "kitsu"])
@pytest.mark.parametrize(
    "kind",
    ["empty", "sparse", "429", "503", "timeout", "invalid_json", "malformed_payload"],
)
def test_dictionary_adapter_contract_is_preserved(provider, kind):
    import metadata

    with patch.object(httpx, "AsyncClient", _client_factory(kind, kind)):
        result = asyncio.run(
            metadata.fetch_chapter_volume_map("mdx-1")
            if provider == "mangadex"
            else metadata.fetch_kitsu_chapter_map("Exact Series", 123, 20)
        )
    assert isinstance(result, dict)
    assert result == ({"1": 1} if kind == "sparse" else {})


@pytest.mark.parametrize("kind", ["empty", "503"])
@pytest.mark.parametrize("apply_changes", [False, True])
def test_unusable_refresh_preserves_manual_map_and_provenance(
    db_path, kind, apply_changes
):
    import metadata_enrichment as enrichment
    import metadata_provenance as provenance
    import metadata_state as state

    _seed_series(
        db_path,
        mangadex_id="mdx-1",
        mal_id=456,
        mu_id="789",
        chapter_vol_map=json.dumps(USABLE),
        chapter_map_source="manual",
        chapter_map_updated_at="2026-01-01",
    )
    provenance.record_manual_metadata(7, {"chapter_vol_map": USABLE})
    with sqlite3.connect(db_path) as db:
        before = db.execute("SELECT * FROM series WHERE id=7").fetchone()
        selection = db.execute(
            "SELECT * FROM series_metadata_fields WHERE series_id=7 AND field_name='chapter_vol_map'"
        ).fetchone()
    with (
        patch.object(httpx, "AsyncClient", _client_factory(kind, "empty")),
        patch("rescan._series_library_dir", return_value=None),
    ):
        assert (
            asyncio.run(enrichment.refresh_mangadex_map(7, apply_changes=apply_changes))
            is False
        )
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT * FROM series WHERE id=7").fetchone() == before
        assert (
            db.execute(
                "SELECT * FROM series_metadata_fields WHERE series_id=7 AND field_name='chapter_vol_map'"
            ).fetchone()
            == selection
        )
    assert state.get_source_states(7)[0]["details"]["preserved_source"] == "manual"


@pytest.mark.parametrize("stage", ["search", "chapters"])
@pytest.mark.parametrize(
    "kind,reason",
    [
        ("429", "http_error"),
        ("503", "http_error"),
        ("timeout", "timeout"),
        ("invalid_json", "invalid_json"),
        ("malformed_payload", "malformed_payload"),
    ],
)
def test_kitsu_checks_search_and_chapter_responses(stage, kind, reason):
    import metadata

    with patch.object(
        httpx, "AsyncClient", _client_factory("empty", kind, kitsu_stage=stage)
    ):
        result = asyncio.run(
            metadata._fetch_kitsu_chapter_map_result("Exact Series", 123, 20)
        )
    assert result.mapping == {}
    assert result.failure_reason == reason
    assert result.http_status == (int(kind) if kind in {"429", "503"} else None)


@pytest.mark.parametrize(
    "provider,payload",
    [
        ("mangadex", {}),
        ("mangadex", []),
        ("mangadex", {"volumes": {"1": []}}),
        ("mangadex", {"volumes": {"1": {"chapters": []}}}),
        ("kitsu", {}),
        ("kitsu", []),
        ("kitsu", {"data": [{"attributes": {}}]}),
        ("kitsu", {"data": [{"id": "7", "attributes": {"titles": []}}]}),
    ],
)
def test_missing_or_malformed_payload_is_not_a_successful_empty_map(provider, payload):
    import metadata

    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with patch.object(
        httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=transport)
    ):
        result = asyncio.run(
            metadata._fetch_chapter_volume_map_result("mdx-1")
            if provider == "mangadex"
            else metadata._fetch_kitsu_chapter_map_result("Exact Series", 123, 20)
        )
    assert result.mapping == {}
    assert result.failure_reason == "malformed_payload"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [], "links": []},
        {"data": [{"attributes": {"number": "1"}}]},
        {"data": [{"attributes": {"number": "1", "volumeNumber": "invalid"}}]},
    ],
)
def test_kitsu_malformed_later_page_discards_partial_map(payload):
    import metadata

    original = httpx.AsyncClient

    def handler(request):
        if request.url.path.endswith("/manga"):
            return httpx.Response(200, json=KITSU_SEARCH)
        if request.url.params["page[offset]"] == "0":
            return httpx.Response(
                200,
                json={
                    "data": [{"attributes": {"number": "1", "volumeNumber": "1"}}],
                    "links": {"next": "https://untrusted.example/never-followed"},
                },
            )
        return httpx.Response(200, json=payload)

    with patch.object(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    ):
        result = asyncio.run(
            metadata._fetch_kitsu_chapter_map_result("Exact Series", 123, 20)
        )
    assert result.mapping == {}
    assert result.failure_reason == "malformed_payload"


def test_kitsu_failed_later_page_discards_partial_map():
    import metadata

    original = httpx.AsyncClient

    def handler(request):
        if request.url.path.endswith("/manga"):
            return httpx.Response(200, json=KITSU_SEARCH)
        if request.url.params["page[offset]"] == "0":
            return httpx.Response(
                200,
                json={
                    "data": [{"attributes": {"number": "1", "volumeNumber": "1"}}],
                    "links": {"next": "next-page"},
                },
            )
        return httpx.Response(503, json={})

    with patch.object(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    ):
        result = asyncio.run(
            metadata._fetch_kitsu_chapter_map_result("Exact Series", 123, 20)
        )
    assert result.mapping == {}
    assert result.failure_reason == "http_error"


@pytest.mark.parametrize("provider", ["mangadex", "kitsu"])
def test_uncollected_chapters_are_successful_empty_observations(provider):
    import metadata

    original = httpx.AsyncClient

    def handler(request):
        if request.url.host == "api.mangadex.org":
            return httpx.Response(
                200, json={"volumes": {"none": {"chapters": {"1": {}}}}}
            )
        if request.url.path.endswith("/manga"):
            return httpx.Response(200, json=KITSU_SEARCH)
        return httpx.Response(
            200, json={"data": [{"attributes": {"number": "1", "volumeNumber": None}}]}
        )

    with patch.object(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    ):
        result = asyncio.run(
            metadata._fetch_chapter_volume_map_result("mdx-1")
            if provider == "mangadex"
            else metadata._fetch_kitsu_chapter_map_result("Exact Series", 123, 20)
        )
    assert result.mapping == {}
    assert result.failure_reason is None


def test_builtin_timeout_remains_a_timeout_failure():
    import metadata

    original = httpx.AsyncClient

    def handler(request):
        raise TimeoutError("private transport context")

    with patch.object(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    ):
        result = asyncio.run(metadata._fetch_chapter_volume_map_result("mdx-1"))
    assert result.failure_reason == "timeout"


@pytest.mark.parametrize("details", ["{}", "[]", "null"])
def test_legacy_degraded_map_requires_explicit_coverage_evidence(db_path, details):
    import metadata_state as state

    _seed_series(db_path)
    state.mark_source_success(7, state.SOURCE_CHAPTER_MAP, degraded=True)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE series_metadata_sources SET details=? WHERE series_id=7", (details,)
        )
    health = state.build_catalog_metadata_health(7)
    assert "provider_failures" in health["issues"]
    assert state.SOURCE_CHAPTER_MAP in health["failed_sources"]
