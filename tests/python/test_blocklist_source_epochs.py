"""Configured source lifetimes through real CRUD and verified HTTP sync."""

import re
from contextlib import closing
import sqlite3
from unittest.mock import patch

import pytest

from test_prowlarr_sync_import import _client, _csrf, _mock_prowlarr


@pytest.fixture
def env(tmp_path, monkeypatch):
    import main
    import security
    import shared

    path = str(tmp_path / "sources.db")
    monkeypatch.setattr(main, "DB_PATH", path)
    monkeypatch.setattr(shared, "DB_PATH", path)
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    security.load_or_create_secret_cipher(str(tmp_path / "keys"))
    old_config = dict(shared.CONFIG)
    main.init_db()
    main.load_config()
    main.ensure_api_key()
    with closing(sqlite3.connect(path)) as db:
        key = db.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()[0]
    client = _client()
    try:
        yield path, client, {"X-Api-Key": key}
    finally:
        client.close()
        shared.CONFIG.clear()
        shared.CONFIG.update(old_config)


def row(env, indexer_id):
    with closing(sqlite3.connect(env[0])) as db:
        db.row_factory = sqlite3.Row
        return dict(
            db.execute("SELECT * FROM indexers WHERE id=?", (indexer_id,)).fetchone()
        )


def scope(value, kind="d"):
    assert isinstance(value, str), "configured source must have an epoch"
    assert re.fullmatch(rf"bs1:{kind}:[0-9a-f]{{64}}", value), "invalid source epoch"
    return value


def create(
    env, *, mode="api", kind="torznab", url="http://source.test", name="Same label"
):
    _, client, headers = env
    if mode == "api":
        response = client.post(
            "/api/v1/indexer",
            json={
                "name": name,
                "implementation": kind,
                "baseUrl": url,
                "apiKey": "fixture-key",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()["indexer"]["id"]
    response = client.post(
        "/indexers",
        data={
            "name": name,
            "type": kind,
            "url": url,
            "api_key": "fixture-key",
        },
        **_csrf(),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    with closing(sqlite3.connect(env[0])) as db:
        return db.execute("SELECT max(id) FROM indexers").fetchone()[0]


def edit(env, indexer_id, mode="api", **values):
    if mode == "api":
        response = env[1].patch(
            f"/api/v1/indexer/{indexer_id}", json=values, headers=env[2]
        )
        assert response.status_code == 200, response.text
    else:
        response = env[1].post(
            f"/indexers/{indexer_id}", data=values, **_csrf(), follow_redirects=False
        )
        assert response.status_code == 303, response.text


def sync(env, parent, protocol="torrent"):
    response_data = [
        {
            "id": 17,
            "name": "Same label",
            "enable": True,
            "protocol": protocol,
            "capabilities": {"categories": [{"id": 7000}]},
        }
    ]
    with patch("httpx.AsyncClient", new=_mock_prowlarr(response_data)):
        response = env[1].post(
            f"/indexers/{parent}/sync-prowlarr",
            data={"selected": "17"},
            **_csrf(),
            follow_redirects=False,
        )
    assert response.status_code == 303, response.text
    with closing(sqlite3.connect(env[0])) as db:
        child = db.execute(
            "SELECT id FROM indexers WHERE parent_prowlarr_id=?", (parent,)
        ).fetchone()
    assert child is not None
    return child[0], response


@pytest.mark.parametrize("mode", ["ui", "api"])
def test_same_label_sources_are_distinct(env, mode):
    first = create(env, mode=mode)
    second = create(env, mode=mode)
    assert scope(row(env, first).get("release_source_scope")) != scope(
        row(env, second).get("release_source_scope")
    )


@pytest.mark.parametrize("mode", ["ui", "api"])
def test_rename_preserves_but_endpoint_edit_rotates(env, mode):
    indexer = create(env, mode=mode)
    initial = scope(row(env, indexer).get("release_source_scope"))
    edit(env, indexer, mode, name="Renamed")
    assert row(env, indexer).get("release_source_scope") == initial
    edit(env, indexer, mode, url="http://other.test")
    changed = scope(row(env, indexer).get("release_source_scope"))
    edit(env, indexer, mode, url="http://source.test")
    restored = scope(row(env, indexer).get("release_source_scope"))
    assert len({initial, changed, restored}) == 3


@pytest.mark.parametrize("mode", ["ui", "api"])
def test_parent_edit_does_not_rebind_old_facade(env, mode):
    parent = create(env, mode=mode, kind="prowlarr")
    child, _ = sync(env, parent)
    root = scope(row(env, parent).get("release_source_scope"), "p")
    before = row(env, child)
    assert before.get("release_source_scope") == f"{root}:17"
    edit(env, parent, mode, url="http://replacement.test")
    assert scope(row(env, parent).get("release_source_scope"), "p") != root
    _, response = sync(env, parent, "usenet")
    after = row(env, child)
    assert (after["url"], after["type"], after.get("release_source_scope")) == (
        before["url"],
        before["type"],
        before.get("release_source_scope"),
    )
    assert "mismatch" in response.headers["location"]


@pytest.mark.parametrize("mode", ["ui", "api"])
def test_non_source_fields_preserve_and_type_edits_rotate(env, mode):
    indexer = create(env, mode=mode)
    original = scope(row(env, indexer).get("release_source_scope"))
    for change in (
        {"priority": 9},
        {"api_key": "rotated-fixture-key"},
        {"categories": "7000" if mode == "ui" else [7000]},
        {"enabled": 0},
        {"use_rss": 0},
        {"use_auto_search": 0},
        {"use_interactive_search": 0},
        {"min_seeders": 9},
        {"tags": "tag" if mode == "ui" else ["tag"]},
    ):
        edit(env, indexer, mode, **change)
        assert row(env, indexer).get("release_source_scope") == original
    edit(env, indexer, mode, type="newznab")
    assert scope(row(env, indexer).get("release_source_scope")) != original


@pytest.mark.parametrize("mode", ["ui", "api"])
def test_effective_endpoint_only_removes_trailing_slashes(env, mode):
    indexer = create(env, mode=mode, url="http://source.test/path///")
    original = scope(row(env, indexer).get("release_source_scope"))
    edit(env, indexer, mode, url="http://source.test/path/")
    assert row(env, indexer).get("release_source_scope") == original
    for url in (
        "http://source.test/path?token=fixture",
        "http://user:fixture@source.test/path",
        "http://source.test/PATH",
    ):
        edit(env, indexer, mode, url=url)
        changed = scope(row(env, indexer).get("release_source_scope"))
        assert changed != original
        original = changed


def test_manual_link_edits_detach_without_trusting_caller_scope(env):
    parent = create(env, kind="prowlarr")
    child, _ = sync(env, parent)
    original = row(env, child)["release_source_scope"]
    edit(env, child, release_source_scope="bs1:d:" + "f" * 64)
    assert row(env, child)["release_source_scope"] == original
    edit(env, child, prowlarrIndexerId=18)
    detached = scope(row(env, child)["release_source_scope"])
    assert detached != original
    edit(env, child, prowlarrIndexerId=17)
    assert scope(row(env, child)["release_source_scope"]) != detached
    # Client-supplied linkage on creation is not verified provider evidence.
    response = env[1].post(
        "/api/v1/indexer",
        json={
            "name": "forged",
            "implementation": "torznab",
            "url": "http://source.test/17",
            "parentProwlarrId": parent,
            "prowlarrIndexerId": 17,
            "release_source_scope": original,
        },
        headers=env[2],
    )
    assert response.status_code == 200
    scope(row(env, response.json()["indexer"]["id"])["release_source_scope"])


def test_parent_delete_retains_facade_and_different_parents_never_alias(env):
    parent = create(env, kind="prowlarr")
    second = create(env, kind="prowlarr")
    child, _ = sync(env, parent)
    sibling, _ = sync(env, second)
    before = row(env, child)
    assert before["release_source_scope"] != row(env, sibling)["release_source_scope"]
    response = env[1].delete(f"/api/v1/indexer/{parent}", headers=env[2])
    assert response.status_code == 200
    assert row(env, child) == before


def test_verified_protocol_correction_preserves_captured_scope(env):
    parent = create(env, kind="prowlarr")
    child, _ = sync(env, parent)
    original = row(env, child)
    sync(env, parent, "usenet")
    corrected = row(env, child)
    assert corrected["type"] == "newznab"
    assert corrected["url"] == original["url"]
    assert corrected["release_source_scope"] == original["release_source_scope"]
    sync(env, parent, "bogus")
    assert row(env, child) == corrected


def test_id_reuse_and_recreation_always_get_new_epochs(env):
    indexer = create(env)
    old = row(env, indexer)["release_source_scope"]
    assert (
        env[1].delete(f"/api/v1/indexer/{indexer}", headers=env[2]).status_code == 200
    )
    replacement = create(env)
    assert row(env, replacement)["release_source_scope"] != old
    # Explicit local PK reuse still has no authority to resurrect the old epoch.
    from release_sources import bind_indexer_scope

    with closing(sqlite3.connect(env[0])) as db, db:
        db.row_factory = sqlite3.Row
        db.execute(
            "INSERT INTO indexers(id,name,type,url) VALUES(?,'reused','torznab','http://source.test')",
            (indexer,),
        )
        assert bind_indexer_scope(db, indexer_id=indexer, previous=None) != old


def test_parent_changed_during_sync_does_not_authorize_import(env):
    parent = create(env, kind="prowlarr")
    from test_prowlarr_sync_import import _MockResp

    class ChangingHTTP:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            from release_sources import bind_indexer_scope

            with closing(sqlite3.connect(env[0])) as db, db:
                db.row_factory = sqlite3.Row
                previous = dict(
                    db.execute(
                        "SELECT * FROM indexers WHERE id=?", (parent,)
                    ).fetchone()
                )
                db.execute(
                    "UPDATE indexers SET url='http://replacement.test' WHERE id=?",
                    (parent,),
                )
                bind_indexer_scope(db, indexer_id=parent, previous=previous)
            return _MockResp(
                200,
                [{"id": 17, "name": "child", "protocol": "torrent", "enable": True}],
            )

    with patch("httpx.AsyncClient", new=ChangingHTTP):
        response = env[1].post(
            f"/indexers/{parent}/sync-prowlarr",
            data={"selected": "17"},
            **_csrf(),
            follow_redirects=False,
        )
    assert response.status_code == 303 and "mismatch" in response.headers["location"]
    with closing(sqlite3.connect(env[0])) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM indexers WHERE parent_prowlarr_id=?", (parent,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("mode", ["ui", "api"])
def test_binding_failure_rolls_back_entire_crud_transaction(env, mode, monkeypatch):
    import release_sources
    import routers.indexers

    indexer = create(env, mode=mode)
    before = row(env, indexer)
    original = release_sources.bind_indexer_scope

    def fail_after_bind(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected scope binding failure")

    target = routers.indexers if mode == "ui" else release_sources
    monkeypatch.setattr(target, "bind_indexer_scope", fail_after_bind)
    with pytest.raises(RuntimeError, match="injected scope binding failure"):
        edit(env, indexer, mode, url="http://replacement.test")
    assert row(env, indexer) == before
    with pytest.raises(RuntimeError, match="injected scope binding failure"):
        create(env, mode=mode)
    with closing(sqlite3.connect(env[0])) as db:
        assert db.execute("SELECT count(*) FROM indexers").fetchone()[0] == 1


@pytest.mark.parametrize("bad_id", [True, 0, -1, 2**63, "17"])
def test_sync_cannot_trust_malformed_upstream_sub_id(env, bad_id):
    parent = create(env, kind="prowlarr")
    data = [{"id": bad_id, "name": "child", "enable": True, "protocol": "torrent"}]
    with patch("httpx.AsyncClient", new=_mock_prowlarr(data)):
        response = env[1].post(
            f"/indexers/{parent}/sync-prowlarr",
            data={"selected": ["1", "17"]},
            **_csrf(),
            follow_redirects=False,
        )
    assert response.status_code == 303
    with closing(sqlite3.connect(env[0])) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM indexers WHERE parent_prowlarr_id=?", (parent,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("protocol", ["missing", None, "", True, "unknown"])
def test_sync_cannot_promote_missing_or_unknown_protocol(env, protocol):
    parent = create(env, kind="prowlarr")
    child = {"id": 17, "name": "child", "enable": True}
    if protocol != "missing":
        child["protocol"] = protocol
    with patch("httpx.AsyncClient", new=_mock_prowlarr([child])):
        response = env[1].post(
            f"/indexers/{parent}/sync-prowlarr",
            data={"selected": "17"},
            **_csrf(),
            follow_redirects=False,
        )
    assert response.status_code == 303
    with closing(sqlite3.connect(env[0])) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM indexers WHERE parent_prowlarr_id=?", (parent,)
            ).fetchone()[0]
            == 0
        )
