"""Request-time attribution through actual transports, DB and grab effects."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import httpx
import pytest
from fastapi.testclient import TestClient

import grab_core
import grab_dedup
import grab_rss
import main
import security
import shared
from clients import GrabResult
from release_identity import (
    SourceContext,
    decode_context,
    identity_for,
    read_history_evidence,
)


@dataclass
class Flow:
    client: TestClient
    headers: dict[str, str]
    requests: list[httpx.Request] = field(default_factory=list)
    submitted: list[str] = field(default_factory=list)
    on_request: Callable[[httpx.Request], None] | None = None
    on_submit: Callable[[], None] | None = None
    raw_protocol: str | None = None
    guid: str = "Release-GUID"
    subs: list[dict[str, Any]] | None = None
    result: GrabResult = field(
        default_factory=lambda: GrabResult(True, "qbittorrent", "flow-hash", True, None)
    )

    def create(self, kind: str = "torznab") -> int:
        response = self.client.post(
            "/api/v1/indexer",
            json={
                "name": "Same label",
                "implementation": kind,
                "baseUrl": "http://192.168.50.10",
                "apiKey": "fixture-only",
            },
            headers=self.headers,
        )
        assert response.status_code == 200, response.text
        return response.json()["indexer"]["id"]

    def edit(self, indexer_id: int, **values: Any) -> None:
        response = self.client.patch(
            f"/api/v1/indexer/{indexer_id}", json=values, headers=self.headers
        )
        assert response.status_code == 200, response.text

    def transport(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(request)
        if request.url.path == "/api/v1/indexer":
            return httpx.Response(200, json=self.subs or [])
        protocol = (
            '<torznab:attr name="downloadProtocol" value="'
            + escape(self.raw_protocol, {'"': "&quot;"})
            + '"/>'
            if self.raw_protocol is not None
            else ""
        )
        suffix = request.url.path.replace("/", "-")
        xml = (
            '<rss xmlns:torznab="http://torznab.com/api/2015/feed"><channel><item>'
            "<title>Test Series Vol 1</title><link>https://download.test/"
            + suffix
            + "</link>"
            "<guid>"
            + escape(self.guid)
            + "</guid>"
            + protocol
            + '<torznab:attr name="seeders" value="10"/>'
            '<torznab:attr name="size" value="1024"/></item></channel></rss>'
        )
        return httpx.Response(200, text=xml)

    async def submit(self, url: str, protocol: str, **kwargs: Any) -> GrabResult:
        self.submitted.append(url)
        # A second writer must remain possible during client I/O.
        with shared.get_db() as db:
            db.execute("UPDATE settings SET value=value WHERE key='api_key'")
        if self.on_submit is not None:
            self.on_submit()
        return self.result

    def search(
        self, *, purpose: str = "auto", series_id: int | None = 1
    ) -> list[dict[str, Any]]:
        return asyncio.run(
            grab_core._search_all("Test Series", purpose=purpose, series_id=series_id)
        )


@pytest.fixture
def flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Flow]:
    path = str(tmp_path / "flow.db")
    monkeypatch.setattr(main, "DB_PATH", path)
    monkeypatch.setattr(shared, "DB_PATH", path)
    monkeypatch.setattr(shared, "CONFIG", {})
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    main.init_db()
    main.load_config()
    main.ensure_api_key()
    with shared.get_db() as db:
        key = db.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()[0]
        db.execute(
            "INSERT INTO series(id,title,search_pattern,monitored) VALUES(1,'Test Series','Test Series',1)"
        )
        db.execute(
            "INSERT INTO volumes(id,series_id,volume_num,status,monitored) VALUES(1,1,1,'wanted',1)"
        )
    client = TestClient(main.app)
    state = Flow(client, {"X-Api-Key": key})
    real_client = httpx.AsyncClient

    def external_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_client(
            *args, **kwargs, transport=httpx.MockTransport(state.transport)
        )

    monkeypatch.setattr(httpx, "AsyncClient", external_client)
    monkeypatch.setattr(grab_core, "grab_url", state.submit)
    grab_dedup._GRABBING_URLS.clear()
    try:
        yield state
    finally:
        client.close()
        grab_dedup._GRABBING_URLS.clear()


def source_row(indexer_id: int) -> dict[str, Any]:
    with shared.get_db() as db:
        return dict(
            db.execute("SELECT * FROM indexers WHERE id=?", (indexer_id,)).fetchone()
        )


def assert_no_acquisition(flow: Flow) -> None:
    assert flow.submitted == []
    with shared.get_db() as db:
        assert db.execute("SELECT count(*) FROM seen").fetchone()[0] == 0
        assert (
            db.execute("SELECT status FROM volumes WHERE id=1").fetchone()[0]
            == "wanted"
        )
        assert (
            db.execute(
                "SELECT count(*) FROM history WHERE event_type='grabbed'"
            ).fetchone()[0]
            == 0
        )


def test_actual_prowlarr_sub_sources_survive_scoring(flow: Flow) -> None:
    parent = flow.create("prowlarr")
    flow.subs = [
        {"id": sub, "name": "Same label", "protocol": "torrent"} for sub in (17, 23)
    ]
    root = source_row(parent)["release_source_scope"]
    items = flow.search(purpose="interactive")
    assert len(items) == 2 and len(flow.requests) == 3
    contexts = [item.get("_source_context") for item in items]
    assert all(isinstance(context, SourceContext) for context in contexts), (
        "actual search must retain concrete source observations"
    )
    assert {
        context.source_scope
        for context in contexts
        if isinstance(context, SourceContext)
    } == {f"{root}:17", f"{root}:23"}
    assert all(
        context.purpose == "interactive" and context.fetch_series_id == 1
        for context in contexts
        if isinstance(context, SourceContext)
    )
    assert all("_score" in item for item in items)


def test_edit_during_fetch_never_relabels(flow: Flow) -> None:
    indexer_id = flow.create()
    original = source_row(indexer_id)["release_source_scope"]
    flow.on_request = lambda request: flow.edit(
        indexer_id, baseUrl="http://192.168.50.11"
    )
    item = flow.search()[0]
    context = item.get("_source_context")
    assert isinstance(context, SourceContext), (
        "fetch must retain its request-time source context"
    )
    assert (
        context.source_scope
        == original
        != source_row(indexer_id)["release_source_scope"]
    )
    accepted = asyncio.run(grab_core.grab_item(item, 1))
    assert (accepted, flow.submitted) == (False, [])
    assert_no_acquisition(flow)
    assert len(flow.requests) == 1


@pytest.mark.parametrize("raw_protocol", [None, "torrent", "unknown", "usenet"])
@pytest.mark.parametrize("mutation", ["endpoint", "delete"])
def test_source_edit_before_handoff_submits_nothing(
    flow: Flow, raw_protocol: str | None, mutation: str
) -> None:
    indexer_id = flow.create()
    flow.raw_protocol = raw_protocol
    flow.guid = "" if raw_protocol is None else "Release-GUID"
    item = flow.search()[0]
    if mutation == "endpoint":
        flow.edit(indexer_id, baseUrl="http://192.168.50.11")
    else:
        response = flow.client.delete(
            f"/api/v1/indexer/{indexer_id}", headers=flow.headers
        )
        assert response.status_code == 200
    accepted = asyncio.run(grab_core.grab_item(item, 1))
    assert (accepted, flow.submitted) == (False, []), (
        "server fetch freshness is mandatory even without complete identity"
    )
    assert_no_acquisition(flow)
    assert len(flow.requests) == 1


@pytest.mark.parametrize("raw_protocol", ["unknown", "usenet"])
def test_incomplete_protocol_remains_compatible_without_scoped_identity(
    flow: Flow, raw_protocol: str
) -> None:
    flow.create()
    flow.raw_protocol = raw_protocol
    item = flow.search()[0]
    assert identity_for(item, 1) is None
    assert asyncio.run(grab_core.grab_item(item, 1))
    assert flow.submitted == [item["url"]]
    with shared.get_db() as db:
        assert db.execute("SELECT release_source_scope FROM seen").fetchone()[0] is None
    assert len(flow.requests) == 1


def pending(flow: Flow) -> dict[str, Any]:
    shared.CONFIG["grab_delay_minutes"] = "60"
    asyncio.run(grab_rss.poll_rss())
    with shared.get_db() as db:
        row = db.execute("SELECT * FROM pending_releases").fetchone()
        assert row is not None
        return dict(row)


def force(flow: Flow, pending_id: int, *, htmx: bool = False) -> httpx.Response:
    token = "source-flow-csrf-" + "x" * 32
    flow.client.cookies.set("csrftoken", token)
    headers = {"X-CSRFToken": token}
    if htmx:
        headers["HX-Request"] = "true"
    return flow.client.post(
        f"/queue/pending/{pending_id}/force-grab",
        headers=headers,
        follow_redirects=False,
    )


def grabbed_history() -> dict[str, Any]:
    with shared.get_db() as db:
        row = db.execute(
            "SELECT * FROM history WHERE event_type='grabbed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        return dict(row)


@pytest.mark.parametrize(
    "guid", ["", "   ", "x" * 4097], ids=["missing", "blank", "oversized"]
)
def test_no_guid_keeps_context_and_url_only_compatibility(
    flow: Flow, guid: str
) -> None:
    indexer_id = flow.create()
    flow.guid = guid
    item = flow.search()[0]
    assert isinstance(item.get("_source_context"), SourceContext)
    assert identity_for(item, 1) is None
    assert asyncio.run(grab_core.grab_item(item, 1))
    assert len(flow.requests) == 1 and flow.submitted == [item["url"]]
    with shared.get_db() as db:
        assert (
            db.execute("SELECT release_source_scope FROM seen").fetchone()[0]
            == source_row(indexer_id)["release_source_scope"]
        )


@pytest.mark.parametrize("htmx", [False, True])
def test_pending_force_grab_preserves_observation(flow: Flow, htmx: bool) -> None:
    indexer_id = flow.create()
    # RSS was not fetched for the eventual target series: do not invent that filter.
    with shared.get_db() as db:
        db.execute(
            "INSERT INTO indexer_tags(indexer_id,tag) VALUES(?,'rss-only')",
            (indexer_id,),
        )
    row = pending(flow)
    assert row["release_context"] is not None, (
        "pending must retain the actual RSS observation"
    )
    context = decode_context(row["release_context"])
    assert (context.purpose, context.fetch_series_id) == ("rss", None)
    assert row["release_source_scope"] == context.source_scope
    assert row["release_guid"] == "Release-GUID"
    response = force(flow, row["id"], htmx=htmx)
    assert response.status_code == (200 if htmx else 303)
    assert flow.submitted == [row["url"]] and len(flow.requests) == 1
    state, evidence = read_history_evidence(grabbed_history())
    assert state == "valid" and evidence is not None
    assert evidence.identity.source_scope == context.source_scope


@pytest.mark.parametrize("raw_protocol", [None, "unknown", "usenet"])
@pytest.mark.parametrize("mutation", ["endpoint", "delete"])
def test_incomplete_pending_stale_source_refuses(
    flow: Flow, raw_protocol: str | None, mutation: str
) -> None:
    indexer_id = flow.create()
    flow.raw_protocol = raw_protocol
    flow.guid = ""
    row = pending(flow)
    if mutation == "endpoint":
        flow.edit(indexer_id, baseUrl="http://192.168.50.11")
    else:
        assert (
            flow.client.delete(
                f"/api/v1/indexer/{indexer_id}", headers=flow.headers
            ).status_code
            == 200
        )
    response = force(flow, row["id"])
    assert response.status_code == 303
    assert_no_acquisition(flow)
    assert len(flow.requests) == 1


@pytest.mark.parametrize("raw_protocol", ["unknown", "usenet"])
def test_incomplete_pending_force_grab_retains_freshness_not_identity(
    flow: Flow, raw_protocol: str
) -> None:
    flow.create()
    flow.raw_protocol = raw_protocol
    row = pending(flow)
    assert row["release_context"] is not None, (
        "incomplete protocol still needs request-freshness evidence"
    )
    assert row["release_source_scope"] is None
    assert force(flow, row["id"]).status_code == 303
    assert flow.submitted == [row["url"]] and len(flow.requests) == 1
    assert read_history_evidence(grabbed_history())[0] == "absent"


@pytest.mark.parametrize(
    "corruption", ["json", "empty", "scope", "protocol", "context-absent"]
)
def test_corrupt_pending_context_refuses(flow: Flow, corruption: str) -> None:
    flow.create()
    row = pending(flow)
    with shared.get_db() as db:
        if corruption in ("json", "empty", "context-absent"):
            db.execute(
                "UPDATE pending_releases SET release_context=?",
                (
                    {"json": '{"version":1}', "empty": "", "context-absent": None}[
                        corruption
                    ],
                ),
            )
        elif corruption == "scope":
            db.execute(
                "UPDATE pending_releases SET release_source_scope=?",
                ("bs1:d:" + "a" * 64,),
            )
        else:
            db.execute("UPDATE pending_releases SET protocol='nzb'")
    assert force(flow, row["id"]).status_code == 303
    assert_no_acquisition(flow)
    assert len(flow.requests) == 1


@pytest.mark.parametrize(
    "mode", ["normal", "insert-conflict", "claim-lost", "untracked"]
)
def test_history_observation_survives_seen_loss(flow: Flow, mode: str) -> None:
    indexer_id = flow.create()
    original = source_row(indexer_id)["release_source_scope"]
    item = flow.search()[0]

    def after_check() -> None:
        flow.edit(indexer_id, baseUrl="http://192.168.50.11")
        with shared.get_db() as db:
            if mode == "insert-conflict":
                db.execute(
                    "INSERT INTO seen(torrent_url,torrent_name) VALUES(?,'other acquisition')",
                    (item["url"],),
                )
            elif mode == "claim-lost":
                db.execute("UPDATE volumes SET download_id='another-owner' WHERE id=1")

    flow.on_submit = after_check
    if mode == "untracked":
        flow.result = GrabResult(False, "qbittorrent", None, True, None)
    accepted = asyncio.run(grab_core.grab_item(item, 1))
    assert accepted is (mode == "normal")
    assert flow.submitted == [item["url"]] and len(flow.requests) == 1
    with shared.get_db() as db:
        seen = db.execute("SELECT * FROM seen").fetchone()
        assert seen is not None
        if mode != "insert-conflict":
            assert seen["release_source_scope"] == original
        db.execute("DELETE FROM seen")
    if mode == "untracked":
        return
    history = grabbed_history()
    state, evidence = read_history_evidence(history)
    assert state == "valid" and evidence is not None
    assert (
        evidence.identity.source_scope
        == original
        != source_row(indexer_id)["release_source_scope"]
    )
    data = json.loads(history["data"])
    assert data["respect_monitoring"] is True
    if mode in ("insert-conflict", "claim-lost"):
        assert data["claim_lost"] is True
    assert history["torrent_url"] == item["url"]


@pytest.mark.parametrize("respect_monitoring", [False, True])
@pytest.mark.parametrize("mutation", ["type", "purpose", "tag", "disabled"])
@pytest.mark.parametrize("raw_protocol", [None, "unknown"])
def test_local_eligibility_races_refuse(
    flow: Flow, respect_monitoring: bool, mutation: str, raw_protocol: str | None
) -> None:
    indexer_id = flow.create()
    flow.raw_protocol = raw_protocol
    item = flow.search(purpose="interactive")[0]
    if mutation == "type":
        flow.edit(indexer_id, implementation="newznab")
    elif mutation == "purpose":
        with shared.get_db() as db:
            db.execute(
                "UPDATE indexers SET use_interactive_search=0 WHERE id=?", (indexer_id,)
            )
    elif mutation == "tag":
        with shared.get_db() as db:
            db.execute(
                "INSERT INTO indexer_tags(indexer_id,tag) VALUES(?,'other')",
                (indexer_id,),
            )
    else:
        flow.edit(indexer_id, enable=False)
    assert not asyncio.run(
        grab_core.grab_item(item, 1, respect_monitoring=respect_monitoring)
    )
    assert_no_acquisition(flow)
    assert len(flow.requests) == 1


@pytest.mark.parametrize("mutation", ["purpose", "type", "disabled"])
def test_pending_original_eligibility_still_required(flow: Flow, mutation: str) -> None:
    indexer_id = flow.create()
    row = pending(flow)
    with shared.get_db() as db:
        if mutation == "purpose":
            db.execute("UPDATE indexers SET use_rss=0 WHERE id=?", (indexer_id,))
        elif mutation == "type":
            # Even an unchanged binding cannot excuse changed implementation.
            db.execute("UPDATE indexers SET type='newznab' WHERE id=?", (indexer_id,))
        else:
            db.execute("UPDATE indexers SET enabled=0 WHERE id=?", (indexer_id,))
    assert force(flow, row["id"]).status_code == 303
    assert_no_acquisition(flow)
    with shared.get_db() as db:
        assert db.execute("SELECT count(*) FROM pending_releases").fetchone()[0] == 1
    assert len(flow.requests) == 1


@pytest.mark.parametrize("raw_protocol", [None, "unknown", "usenet"])
def test_regular_expired_delay_uses_fresh_item(
    flow: Flow, raw_protocol: str | None
) -> None:
    indexer_id = flow.create()
    flow.raw_protocol = raw_protocol
    old = pending(flow)
    flow.edit(indexer_id, baseUrl="http://192.168.50.11")
    flow.guid = "Fresh-GUID"
    current_scope = source_row(indexer_id)["release_source_scope"]
    with shared.get_db() as db:
        db.execute("UPDATE pending_releases SET first_seen=datetime('now','-2 hours')")
    asyncio.run(grab_rss.poll_rss())
    assert flow.submitted == [old["url"]] and len(flow.requests) == 2
    with shared.get_db() as db:
        assert db.execute("SELECT count(*) FROM pending_releases").fetchone()[0] == 0
        seen = db.execute("SELECT * FROM seen").fetchone()
        assert seen is not None and seen["release_guid"] == "Fresh-GUID"
        assert seen["release_source_scope"] == (
            current_scope if raw_protocol is None else None
        )


@pytest.mark.parametrize(
    "guid", ["", "   ", "x" * 4097], ids=["missing", "blank", "oversized"]
)
def test_no_guid_pending_force_grab_is_not_rejected(flow: Flow, guid: str) -> None:
    flow.create()
    flow.guid = guid
    row = pending(flow)
    assert isinstance(decode_context(row["release_context"]), SourceContext)
    assert force(flow, row["id"]).status_code == 303
    assert flow.submitted == [row["url"]] and len(flow.requests) == 1
    assert read_history_evidence(grabbed_history())[0] == "absent"


@pytest.mark.parametrize("raw_protocol", [None, "unknown"])
def test_failed_server_capture_never_becomes_legacy(
    flow: Flow, raw_protocol: str | None
) -> None:
    indexer_id = flow.create()
    flow.raw_protocol = raw_protocol
    with shared.get_db() as db:
        db.execute(
            "UPDATE indexers SET release_source_scope=NULL WHERE id=?", (indexer_id,)
        )
    item = flow.search()[0]
    assert not asyncio.run(grab_core.grab_item(item, 1))
    row = pending(flow)
    assert row["release_context"] is not None
    assert force(flow, row["id"]).status_code == 303
    assert_no_acquisition(flow)
    assert len(flow.requests) == 2


@pytest.mark.parametrize("evidence", ["absent", "dictionary"])
def test_legacy_url_only_does_not_trust_caller_context(
    flow: Flow, evidence: str
) -> None:
    indexer_id = flow.create()
    item: dict[str, Any] = {
        "url": "https://download.test/legacy",
        "title": "Test Series Vol 1",
        "protocol": "torrent",
        "guid": "Release-GUID",
    }
    if evidence == "dictionary":
        item["_source_context"] = {
            "indexer_id": indexer_id,
            "row_scope": source_row(indexer_id)["release_source_scope"],
            "implementation": "torznab",
            "protocol": "torrent",
            "purpose": "auto",
            "fetch_series_id": 1,
            "sub_id": None,
        }
    assert identity_for(item, 1) is None
    assert asyncio.run(grab_core.grab_item(item, 1))
    assert flow.submitted == [item["url"]] and flow.requests == []
    with shared.get_db() as db:
        assert db.execute("SELECT release_source_scope FROM seen").fetchone()[0] is None
    assert read_history_evidence(grabbed_history())[0] == "absent"


@pytest.mark.parametrize("htmx", [False, True])
def test_legacy_pending_wholly_absent_evidence_remains_url_only(
    flow: Flow, htmx: bool
) -> None:
    with shared.get_db() as db:
        cursor = db.execute(
            "INSERT INTO pending_releases(series_id,url,title,protocol) VALUES(1,'https://download.test/legacy','Test Series Vol 1','torrent')"
        )
        pending_id = cursor.lastrowid
        assert pending_id is not None
    assert force(flow, pending_id, htmx=htmx).status_code == (200 if htmx else 303)
    assert flow.submitted == ["https://download.test/legacy"] and flow.requests == []
    assert read_history_evidence(grabbed_history())[0] == "absent"


@pytest.mark.parametrize("listed_protocol", [None, "unknown", "torrent", "usenet"])
@pytest.mark.parametrize("raw_protocol", [None, "unknown", "torrent", "usenet"])
def test_prowlarr_protocol_requires_list_and_xml_agreement(
    flow: Flow, listed_protocol: str | None, raw_protocol: str | None
) -> None:
    parent = flow.create("prowlarr")
    flow.subs = [{"id": 17, "name": "Same label", "protocol": listed_protocol}]
    flow.raw_protocol = raw_protocol
    item = flow.search()[0]
    expected = {"torrent": "torrent", "usenet": "nzb"}.get(listed_protocol or "")
    observed = (
        {"torrent": "torrent", "usenet": "nzb"}.get(raw_protocol or "")
        if raw_protocol is not None
        else expected
    )
    identity = identity_for(item, 1)
    if expected is not None and expected == observed:
        assert identity is not None
        assert (
            identity.source_scope == source_row(parent)["release_source_scope"] + ":17"
        )
    else:
        assert identity is None and not isinstance(
            item.get("_source_context"), SourceContext
        )
    flow.edit(parent, baseUrl="http://192.168.50.11")
    assert not asyncio.run(grab_core.grab_item(item, 1))
    assert_no_acquisition(flow)
    assert len(flow.requests) == 2


@pytest.mark.parametrize("sub_id", [True, "17", 0, -1, 2**63])
def test_prowlarr_sub_id_is_not_coerced(flow: Flow, sub_id: object) -> None:
    flow.create("prowlarr")
    flow.subs = [{"id": sub_id, "name": "Same label", "protocol": "torrent"}]
    assert flow.search() == []
    assert len(flow.requests) == 1
    assert_no_acquisition(flow)


def test_parent_edit_during_list_keeps_original_sub_request(flow: Flow) -> None:
    parent = flow.create("prowlarr")
    original = source_row(parent)["release_source_scope"]
    flow.subs = [{"id": 17, "name": "Same label", "protocol": "torrent"}]

    def edit_during_list(request: httpx.Request) -> None:
        if request.url.path == "/api/v1/indexer":
            flow.edit(parent, baseUrl="http://192.168.50.11")

    flow.on_request = edit_during_list
    item = flow.search()[0]
    identity = identity_for(item, 1)
    assert identity is not None and identity.source_scope == original + ":17"
    assert all(request.url.host == "192.168.50.10" for request in flow.requests)
    assert not asyncio.run(grab_core.grab_item(item, 1))
    assert_no_acquisition(flow)
    assert len(flow.requests) == 2


@pytest.mark.parametrize(
    "corruption",
    [
        "sub-null",
        "sub-bool",
        "id-bool",
        "series-bool",
        "purpose",
        "implementation",
        "scope",
        "version-bool",
        "protocol",
        "extra",
        "duplicate",
    ],
)
def test_malformed_freshness_pending_never_downgrades(
    flow: Flow, corruption: str
) -> None:
    flow.create("prowlarr")
    flow.subs = [{"id": 17, "name": "Same label", "protocol": "unknown"}]
    row = pending(flow)
    envelope = json.loads(row["release_context"])
    request = envelope["request"]
    if corruption == "sub-null":
        request["sub_id"] = None
    elif corruption == "sub-bool":
        request["sub_id"] = True
    elif corruption == "id-bool":
        request["indexer_id"] = True
    elif corruption == "series-bool":
        request["fetch_series_id"] = False
    elif corruption == "purpose":
        request["purpose"] = "search"
    elif corruption == "implementation":
        request["implementation"] = False
    elif corruption == "scope":
        request["row_scope"] = "bs1:p:" + "a" * 64 + ":17"
    elif corruption == "version-bool":
        envelope["version"] = True
    elif corruption == "protocol":
        envelope["protocol"] = "unknown"
    elif corruption == "extra":
        request["observed"] = True
    raw = json.dumps(envelope)
    if corruption == "duplicate":
        raw = raw.replace('"version": 1', '"version": 1, "version": 1')
    with shared.get_db() as db:
        db.execute("UPDATE pending_releases SET release_context=?", (raw,))
    assert force(flow, row["id"]).status_code == 303
    assert_no_acquisition(flow)
    with shared.get_db() as db:
        assert db.execute("SELECT count(*) FROM pending_releases").fetchone()[0] == 1
    assert len(flow.requests) == 2


@pytest.mark.parametrize(
    "raw_protocol", [None, "usenet", "nzb", "torrent", "", "unknown"]
)
def test_newznab_omission_is_known_but_contradiction_is_not(
    flow: Flow, raw_protocol: str | None
) -> None:
    indexer_id = flow.create("newznab")
    flow.raw_protocol = raw_protocol
    item = flow.search()[0]
    identity = identity_for(item, 1)
    if raw_protocol in (None, "usenet", "nzb"):
        assert identity is not None and identity.protocol == "nzb"
        assert identity.source_scope == source_row(indexer_id)["release_source_scope"]
    else:
        assert identity is None
    assert asyncio.run(grab_core.grab_item(item, 1))
    assert flow.submitted == [item["url"]] and len(flow.requests) == 1


def test_rss_siblings_retain_concrete_scopes(flow: Flow) -> None:
    parent = flow.create("prowlarr")
    flow.subs = [
        {"id": sub, "name": "Same label", "protocol": "torrent"} for sub in (17, 23)
    ]

    async def fetch() -> list[dict[str, Any]]:
        from routers.indexers import fetch_all_rss

        with shared.get_db() as db:
            return await fetch_all_rss(db)

    items = grab_core._collect_and_score(asyncio.run(fetch()), set())
    root = source_row(parent)["release_source_scope"]
    contexts = [item.get("_source_context") for item in items]
    assert all(isinstance(context, SourceContext) for context in contexts)
    assert {
        context.source_scope
        for context in contexts
        if isinstance(context, SourceContext)
    } == {root + ":17", root + ":23"}
    assert all(
        (context.purpose, context.fetch_series_id) == ("rss", None)
        for context in contexts
        if isinstance(context, SourceContext)
    )
    assert len(flow.requests) == 3


def test_present_request_dictionary_is_not_promoted(flow: Flow) -> None:
    indexer_id = flow.create()
    item: dict[str, Any] = {
        "url": "https://download.test/forged",
        "title": "Test Series Vol 1",
        "protocol": "torrent",
        "_source_request": {
            "indexer_id": indexer_id,
            "row_scope": source_row(indexer_id)["release_source_scope"],
        },
    }
    assert not asyncio.run(grab_core.grab_item(item, 1))
    assert_no_acquisition(flow)
    assert flow.requests == []


@pytest.mark.parametrize("guid", ["   ", "x" * 4097], ids=["blank", "oversized"])
@pytest.mark.parametrize("mode", ["live", "pending"])
@pytest.mark.parametrize("mutation", ["endpoint", "delete"])
def test_incomplete_guid_source_races(
    flow: Flow, guid: str, mode: str, mutation: str
) -> None:
    indexer_id = flow.create()
    flow.guid = guid
    item = flow.search()[0] if mode == "live" else pending(flow)
    if mutation == "endpoint":
        flow.edit(indexer_id, baseUrl="http://192.168.50.11")
    else:
        assert (
            flow.client.delete(
                f"/api/v1/indexer/{indexer_id}", headers=flow.headers
            ).status_code
            == 200
        )
    if mode == "live":
        assert not asyncio.run(grab_core.grab_item(item, 1))
    else:
        assert force(flow, item["id"]).status_code == 303
    assert_no_acquisition(flow)
    assert len(flow.requests) == 1


def test_observation_does_not_change_global_seen_guid_dedup(flow: Flow) -> None:
    flow.create("prowlarr")
    flow.subs = [
        {"id": sub, "name": "Same label", "protocol": "torrent"} for sub in (17, 23)
    ]
    first, second = flow.search()
    assert identity_for(first, 1) != identity_for(second, 1)
    assert asyncio.run(grab_core.grab_item(first, 1))
    assert not asyncio.run(grab_core.grab_item(second, 1))
    assert flow.submitted == [first["url"]] and len(flow.requests) == 3
    with shared.get_db() as db:
        assert db.execute("SELECT count(*) FROM seen").fetchone()[0] == 1


def test_actual_manual_http_does_not_promote_context_dictionary(flow: Flow) -> None:
    indexer_id = flow.create()
    response = flow.client.post(
        "/api/series/1/volumes/1/grab-release",
        json={
            "url": "https://download.test/manual",
            "title": "Test Series Vol 1",
            "protocol": "torrent",
            "guid": "Release-GUID",
            "_source_context": {
                "indexer_id": indexer_id,
                "row_scope": source_row(indexer_id)["release_source_scope"],
                "implementation": "torznab",
                "protocol": "torrent",
                "purpose": "auto",
                "fetch_series_id": 1,
                "sub_id": None,
            },
        },
        headers=flow.headers,
    )
    assert response.status_code == 200 and response.json()["ok"] is True
    assert flow.submitted == ["https://download.test/manual"] and flow.requests == []
    with shared.get_db() as db:
        assert db.execute("SELECT release_source_scope FROM seen").fetchone()[0] is None
    assert read_history_evidence(grabbed_history())[0] == "absent"


@pytest.mark.parametrize(
    "guid", [None, 17, False, []], ids=["null", "integer", "boolean", "array"]
)
def test_nonstring_guid_cannot_create_identity(flow: Flow, guid: object) -> None:
    indexer_id = flow.create()
    item = flow.search()[0]
    item["guid"] = guid
    assert identity_for(item, 1) is None
    assert asyncio.run(grab_core.grab_item(item, 1))
    with shared.get_db() as db:
        row = db.execute(
            "SELECT release_guid,release_source_scope FROM seen"
        ).fetchone()
        assert row is not None and row["release_guid"] is None
        assert (
            row["release_source_scope"]
            == source_row(indexer_id)["release_source_scope"]
        )
    assert read_history_evidence(grabbed_history())[0] == "absent"
    assert flow.submitted == [item["url"]] and len(flow.requests) == 1
