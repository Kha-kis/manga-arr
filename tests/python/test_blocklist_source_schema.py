"""Durable evidence, strict values, and real additive/rebuild migrations."""

from collections.abc import Iterator
from contextlib import closing
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any

import pytest

from release_identity import (
    ReleaseIdentity,
    SourceContext,
    add_block,
    decode_context,
    encode_context,
    identity_for,
    is_blocked,
    parse_scope,
    read_history_evidence,
)
from release_sources import capture_source_context, source_context_is_current

DIRECT = "bs1:d:" + "a" * 64
ROOT = "bs1:p:" + "b" * 64


@pytest.fixture
def path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from file_identity_schema7 import initialize_schema7
    import shared

    path = tmp_path / "foundation.db"
    monkeypatch.setattr(shared, "DB_PATH", str(path))
    initialize_schema7(path, monkeypatch)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO series(id,title,search_pattern) VALUES(1,'one','one'),(2,'two','two')"
        )
        db.execute("INSERT INTO volumes(id,series_id,volume_num) VALUES(1,1,1)")
    return path


@pytest.fixture
def db(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        yield db


def block(
    db, *, url="url-a", identities=(), series_id=1, title="first", reason="original"
):
    return add_block(
        db,
        series_id=series_id,
        url=url,
        title=title,
        reason=reason,
        indexer="same-label",
        protocol="torrent",
        size_bytes=12,
        identities=identities,
    )


def test_fresh_schema_constraints_indexes_and_nullable_evidence(db):
    assert db.execute("PRAGMA user_version").fetchone()[0] == 7
    for table, names in (
        ("seen", {"release_source_scope"}),
        (
            "pending_releases",
            {"release_source_scope", "release_guid", "release_context"},
        ),
        ("indexers", {"release_source_scope"}),
    ):
        columns = {row[1]: row for row in db.execute(f"PRAGMA table_info({table})")}
        assert names <= columns.keys()
        assert all(columns[name][3] == 0 for name in names)
    fks = db.execute("PRAGMA foreign_key_list(blocklist_release_identities)").fetchall()
    assert {(r[2], r[3], r[6]) for r in fks} == {
        ("blocklist", "blocklist_id", "CASCADE"),
        ("series", "series_id", "CASCADE"),
    }
    assert all(
        r[3] for r in db.execute("PRAGMA table_info(blocklist_release_identities)")
    )
    assert {
        r[2] for r in db.execute("PRAGMA foreign_key_list(release_selection_evidence)")
    } == {"auth_sessions", "series", "volumes"}
    plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM blocklist_release_identities "
        "WHERE series_id=? AND source_scope=? AND protocol=? AND release_guid=?",
        (1, DIRECT, "torrent", "Guid"),
    ).fetchall()
    assert any("idx_blocklist_release_identity" in r[3] for r in plan)
    assert (
        len(db.execute("PRAGMA index_list(release_selection_evidence)").fetchall()) == 4
    )


def test_repeated_anchor_additional_tuples_no_refresh_or_erasure(db):
    first = ReleaseIdentity(1, DIRECT, "torrent", " Guid ")
    second = ReleaseIdentity(2, ROOT + ":17", "nzb", "Guid")
    anchor = block(db, identities=[first])
    db.execute("UPDATE blocklist SET added_at='2000-01-01' WHERE id=?", (anchor,))
    before = db.execute("SELECT * FROM blocklist").fetchall()
    for identities in ([first], [first, second], []):
        assert (
            block(db, identities=identities, title="changed", reason="changed")
            == anchor
        )
    assert db.execute("SELECT * FROM blocklist").fetchall() == before
    assert (
        db.execute("SELECT count(*) FROM blocklist_release_identities").fetchone()[0]
        == 2
    )
    assert is_blocked(db, url="rotated", identity=first)
    assert is_blocked(db, url="rotated", identity=second)
    assert is_blocked(db, url="url-a", identity=None)
    assert not is_blocked(
        db, url="rotated", identity=ReleaseIdentity(1, DIRECT, "torrent", "guid")
    )
    assert not is_blocked(
        db, url="rotated", identity=ReleaseIdentity(1, DIRECT, "nzb", "Guid")
    )
    assert not is_blocked(
        db, url="rotated", identity=ReleaseIdentity(2, DIRECT, "torrent", "Guid")
    )
    assert not is_blocked(
        db,
        url="rotated",
        identity=ReleaseIdentity(1, "bs1:d:" + "c" * 64, "torrent", "Guid"),
    )


def test_multiple_anchors_ttl_and_series_cascades(db):
    identity = ReleaseIdentity(2, DIRECT, "torrent", "Guid")
    block(db, identities=[identity], series_id=1)
    block(db, url="url-b", identities=[identity], series_id=2)
    db.execute("DELETE FROM blocklist WHERE torrent_url='url-a'")
    assert is_blocked(db, url="rotated", identity=identity)
    db.execute("DELETE FROM series WHERE id=2")
    assert not is_blocked(db, url="rotated", identity=identity)
    assert db.execute("SELECT count(*) FROM blocklist").fetchone()[0] == 0
    db.execute("INSERT INTO series(id,title,search_pattern) VALUES(2,'two','two')")
    block(db, identities=[identity], series_id=1)
    db.execute("DELETE FROM series WHERE id=2")
    assert db.execute("SELECT count(*) FROM blocklist").fetchone()[0] == 1
    assert (
        db.execute("SELECT count(*) FROM blocklist_release_identities").fetchone()[0]
        == 0
    )
    db.execute("INSERT INTO series(id,title,search_pattern) VALUES(2,'two','two')")
    block(db, identities=[identity], series_id=1)
    db.execute("DELETE FROM series WHERE id=1")
    assert db.execute("SELECT count(*) FROM blocklist").fetchone()[0] == 0
    assert (
        db.execute("SELECT count(*) FROM blocklist_release_identities").fetchone()[0]
        == 0
    )


def test_helpers_never_commit_and_deleted_anchor_gets_new_lifetime(db):
    block(db, identities=[ReleaseIdentity(1, DIRECT, "torrent", "g")])
    db.rollback()
    assert db.execute("SELECT * FROM blocklist").fetchall() == []
    old = block(db)
    db.execute("UPDATE blocklist SET added_at='2000-01-01'")
    db.execute("DELETE FROM blocklist WHERE added_at < datetime('now','-90 days')")
    new = block(db)
    assert new != old
    assert db.execute("SELECT added_at FROM blocklist").fetchone()[0] != "2000-01-01"


@pytest.mark.parametrize(
    "scope",
    [
        None,
        "Nyaa",
        ROOT,
        DIRECT.upper(),
        DIRECT + ":1",
        ROOT + ":0",
        ROOT + ":01",
        ROOT + ":-1",
        ROOT + ":9223372036854775808",
    ],
)
def test_strict_scope_rejects_unobserved_or_noncanonical_values(scope):
    with pytest.raises(ValueError):
        ReleaseIdentity(1, scope, "torrent", "g")


@pytest.mark.parametrize(
    "guid", [None, 7, True, "", " \t ", "x" * 4097, "é" * 2049, "\ud800"]
)
def test_invalid_guid_never_creates_scoped_identity(guid):
    item = {
        "_source_context": SourceContext(
            1, DIRECT, "torznab", None, "torrent", "rss", None
        ),
        "protocol": "torrent",
        "guid": guid,
    }
    assert identity_for(item, 1) is None
    assert identity_for({**item, "_source_context": {"row_scope": DIRECT}}, 1) is None


def test_guid_boundary_case_and_frozen_values():
    identity = ReleaseIdentity(1, DIRECT, "torrent", " é" + "x" * 4094 + " ")
    assert len(identity.release_guid.encode()) == 4096
    with pytest.raises(FrozenInstanceError):
        setattr(identity, "series_id", 2)
    with pytest.raises(ValueError):
        ReleaseIdentity(True, DIRECT, "torrent", "guid")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "torrent"),
        ("torrent", "torrent"),
        ("usenet", None),
        ("bogus", None),
        (False, None),
        ("", None),
    ],
)
def test_capture_direct_protocol_is_evidence_not_permissive_default(raw, expected):
    context = capture_source_context(
        {"id": 1, "type": "torznab", "release_source_scope": DIRECT},
        purpose="rss",
        fetch_series_id=None,
        observed_protocol=raw,
    )
    assert (context.protocol if context else None) == expected


def test_parent_and_facade_context_codec_and_identity():
    parent = {"id": 1, "type": "prowlarr", "release_source_scope": ROOT}
    context = capture_source_context(
        parent,
        purpose="interactive",
        fetch_series_id=2,
        sub_id=17,
        observed_protocol="usenet",
    )
    assert context is not None
    assert decode_context(encode_context(context)) == context
    assert identity_for(
        {"_source_context": context, "protocol": "nzb", "guid": " G "}, 2
    ) == ReleaseIdentity(2, ROOT + ":17", "nzb", "G")
    invalid_subs: list[Any] = [True, 0, -1, 2**63, "17", None]
    for sub in invalid_subs:
        assert (
            capture_source_context(
                parent,
                purpose="rss",
                fetch_series_id=None,
                sub_id=sub,
                observed_protocol="torrent",
            )
            is None
        )
    facade = capture_source_context(
        {"id": 2, "type": "newznab", "release_source_scope": ROOT + ":17"},
        purpose="rss",
        fetch_series_id=None,
    )
    assert facade is not None and facade.source_scope == context.source_scope
    assert facade.sub_id == 17


@pytest.mark.parametrize(
    "mutation",
    [
        {"version": True},
        {"version": 2},
        {"indexer_id": True},
        {"fetch_series_id": False},
        {"sub_id": 17},
        {"implementation": "unknown"},
        {"protocol": "usenet"},
        {"purpose": "unknown"},
        {"row_scope": ROOT},
        {"extra": 1},
    ],
)
def test_context_json_rejects_invalid_scalar_types_and_shape(mutation):
    value = json.loads(
        encode_context(
            SourceContext(1, DIRECT, "torznab", None, "torrent", "rss", None)
        )
    )
    with pytest.raises(ValueError):
        decode_context(json.dumps({**value, **mutation}))


def test_context_rejects_duplicate_keys_and_missing_fields():
    encoded = encode_context(
        SourceContext(1, DIRECT, "torznab", None, "torrent", "rss", None)
    )
    with pytest.raises(ValueError):
        decode_context(encoded[:-1] + ',"version":1}')
    for value in ("{}", "null", "[]", "not-json"):
        with pytest.raises(ValueError):
            decode_context(value)


def test_source_revalidation_uses_original_purpose_series_binding_and_type(db):
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO indexers(id,name,type,url,release_source_scope) VALUES(1,'test','torznab','http://test',?)",
        (DIRECT,),
    )
    context = capture_source_context(
        dict(db.execute("SELECT * FROM indexers").fetchone()),
        purpose="rss",
        fetch_series_id=None,
    )
    assert context is not None and source_context_is_current(db, context)
    db.execute("INSERT INTO indexer_tags VALUES(1,'restricted')")
    assert source_context_is_current(db, context), (
        "RSS must retain its original null series filter"
    )
    filtered = capture_source_context(
        dict(db.execute("SELECT * FROM indexers").fetchone()),
        purpose="auto",
        fetch_series_id=1,
    )
    assert filtered is not None and not source_context_is_current(db, filtered)
    db.execute("INSERT INTO series_tags(series_id,tag) VALUES(1,'restricted')")
    assert source_context_is_current(db, filtered)
    db.execute("UPDATE indexers SET use_rss=0")
    assert not source_context_is_current(db, context)
    db.execute("UPDATE indexers SET use_rss=1,type='newznab'")
    assert not source_context_is_current(db, context)
    db.execute(
        "UPDATE indexers SET type='torznab',release_source_scope=?",
        ("bs1:d:" + "f" * 64,),
    )
    assert not source_context_is_current(db, context)
    db.execute("DELETE FROM indexers")
    assert not source_context_is_current(db, context)


def test_history_tristate_and_outer_agreement():
    value = {
        "version": 1,
        "series_id": 1,
        "url": "url",
        "source_scope": DIRECT,
        "protocol": "torrent",
        "release_guid": " G ",
    }
    row = {
        "data": json.dumps({"release_identity": value}),
        "series_id": 1,
        "torrent_url": "url",
        "protocol": "torrent",
    }
    state, evidence = read_history_evidence(row)
    assert (
        state == "valid"
        and evidence is not None
        and evidence.identity.release_guid == "G"
    )
    assert read_history_evidence({"data": "{}"}) == ("absent", None)
    assert read_history_evidence({}) == ("absent", None)
    for outer in (
        {"series_id": 2},
        {"series_id": True},
        {"torrent_url": "other"},
        {"protocol": "nzb"},
        {"data": "{"},
    ):
        assert read_history_evidence({**row, **outer}) == ("invalid", None)
    for change in ({"version": True}, {"source_scope": ROOT}, {"release_guid": ""}):
        assert read_history_evidence(
            {"data": json.dumps({"release_identity": {**value, **change}})}
        ) == ("invalid", None)


@pytest.mark.parametrize("version", [0, 6])
def test_real_rebuild_and_additive_upgrade_preserve_all_evidence(path, version):
    import schema

    context = encode_context(
        SourceContext(1, DIRECT, "torznab", None, "torrent", "rss", None)
    )
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO indexers(id,name,type,url,release_source_scope) VALUES(1,'same','torznab','http://test',?)",
            (DIRECT,),
        )
        db.execute(
            "INSERT INTO seen(torrent_url,series_id,release_guid,release_source_scope) VALUES('seen',1,'G',?)",
            (DIRECT,),
        )
        db.execute(
            "INSERT INTO pending_releases(series_id,url,release_guid,release_source_scope,release_context) VALUES(1,'pending','G',?,?)",
            (DIRECT, context),
        )
        identity = ReleaseIdentity(1, DIRECT, "torrent", "G")
        block(db, identities=[identity])
        db.execute(
            "INSERT INTO release_selection_evidence VALUES(?, 'api', NULL, 100, 1000, 1, 1, ?)",
            ("a" * 64, '{ "retained": "bytes" }'),
        )
        db.execute(f"PRAGMA user_version={version}")
    schema.init_db()
    schema._migrate_schema_constraints()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8
        assert (
            db.execute("SELECT release_source_scope FROM indexers").fetchone()[0]
            == DIRECT
        )
        assert db.execute(
            "SELECT release_guid,release_source_scope FROM seen"
        ).fetchone() == ("G", DIRECT)
        assert db.execute(
            "SELECT release_guid,release_source_scope,release_context FROM pending_releases"
        ).fetchone() == ("G", DIRECT, context)
        assert is_blocked(db, url="rotated", identity=identity)
        assert db.execute(
            "SELECT snapshot_json FROM release_selection_evidence"
        ).fetchone() == ('{ "retained": "bytes" }',)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_genuine_pre_feature_upgrade_initializes_only_current_configuration(path):
    import schema

    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE blocklist_release_identities")
        db.execute("DROP TABLE release_selection_evidence")
        for table, columns in (
            ("indexers", ["release_source_scope"]),
            ("seen", ["release_source_scope"]),
            (
                "pending_releases",
                ["release_source_scope", "release_guid", "release_context"],
            ),
        ):
            for column in columns:
                db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        db.execute(
            "INSERT INTO indexers(id,name,type,url) VALUES(1,'same','prowlarr','http://parent')"
        )
        db.execute(
            "INSERT INTO indexers(id,name,type,url,parent_prowlarr_id,prowlarr_indexer_id) VALUES(2,'same','torznab','http://parent/17',1,17),(3,'same','torznab','http://old/17',1,17)"
        )
        db.execute(
            "INSERT INTO seen(torrent_url,series_id,release_guid,indexer) VALUES('seen',1,'G','Nyaa')"
        )
        db.execute(
            "INSERT INTO pending_releases(series_id,url,indexer) VALUES(1,'pending','Nyaa')"
        )
        db.execute(
            "INSERT INTO blocklist(series_id,torrent_url,torrent_name,indexer) VALUES(1,'url-a','legacy','Nyaa')"
        )
        db.execute("PRAGMA user_version=6")
    schema.init_db()
    with closing(sqlite3.connect(path)) as db:
        scopes = dict(db.execute("SELECT id,release_source_scope FROM indexers"))
        parse_scope(scopes[1], allow_root=True)
        assert scopes[2] == scopes[1] + ":17"
        assert scopes[3].startswith("bs1:d:")
        assert db.execute("SELECT release_source_scope FROM seen").fetchone() == (None,)
        assert db.execute(
            "SELECT release_source_scope,release_guid,release_context FROM pending_releases"
        ).fetchone() == (None, None, None)
        assert db.execute("SELECT * FROM blocklist_release_identities").fetchall() == []
    schema.init_db()
    with closing(sqlite3.connect(path)) as db:
        assert (
            dict(db.execute("SELECT id,release_source_scope FROM indexers")) == scopes
        )


def test_migration_failure_rolls_back_new_tables_columns_epochs_and_revision(
    path, monkeypatch
):
    import schema
    import release_sources

    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE blocklist_release_identities")
        db.execute("DROP TABLE release_selection_evidence")
        db.execute("ALTER TABLE indexers DROP COLUMN release_source_scope")
        db.execute("ALTER TABLE seen DROP COLUMN release_source_scope")
        for column in ("release_source_scope", "release_guid", "release_context"):
            db.execute(f"ALTER TABLE pending_releases DROP COLUMN {column}")
        db.execute(
            "INSERT INTO indexers(name,type,url) VALUES('one','torznab','http://one'),('two','torznab','http://two')"
        )
        db.execute("PRAGMA user_version=6")
        before = tuple(db.iterdump())
    original = release_sources.bind_indexer_scope
    calls = []

    def fail_after_binding(*args, **kwargs):
        scope = original(*args, **kwargs)
        calls.append(scope)
        if len(calls) == 2:
            raise RuntimeError("injected migration failure")
        return scope

    monkeypatch.setattr(release_sources, "bind_indexer_scope", fail_after_binding)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        schema.init_db()
    assert len(calls) == 2 and calls[0] != calls[1]
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert tuple(db.iterdump()) == before


def test_future_version_refusal_is_non_mutating(path):
    import schema

    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA user_version=9")
        before = tuple(db.iterdump())
    with pytest.raises(RuntimeError, match="refusing to modify"):
        schema.init_db()
    with closing(sqlite3.connect(path)) as db:
        assert tuple(db.iterdump()) == before


def test_previous_binary_refuses_revision7_before_writes(path):
    from test_file_identity_schema import _V5_VALIDATOR

    program = (
        "import sqlite3,sys\nfrom typing import cast\n_SUPPORTED_SCHEMA_VERSION=6\n"
        + _V5_VALIDATOR
        + "\ndb=sqlite3.connect(sys.argv[1])\ntry:\n _validate_schema_version(db)\n"
        "except RuntimeError:\n sys.exit(0)\nraise AssertionError('old binary accepted new DB')\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_selection_checks_and_session_cascade(db):
    db.execute("INSERT INTO auth_admin VALUES(1,'fixture','fixture-hash','0','0')")
    db.execute(
        "INSERT INTO auth_sessions(token_hash,admin_id,created_at,expires_at,last_seen_at) VALUES(?,1,0,9999,0)",
        ("b" * 64,),
    )
    sql = "INSERT INTO release_selection_evidence VALUES(?,?,?,?,?,?,?,?)"
    valid = ["a" * 64, "browser", "b" * 64, 100, 1000, 1, 1, "{}"]
    for index, bad in (
        (0, "not-a-digest"),
        (1, "other"),
        (2, None),
        (4, 1001),
        (7, "x" * 16385),
    ):
        values = valid.copy()
        values[index] = bad
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, values)
    db.execute(sql, valid)
    db.execute("DELETE FROM auth_sessions")
    assert db.execute("SELECT * FROM release_selection_evidence").fetchall() == []
    db.execute(sql, ["a" * 64, "api", None, 100, 1000, 1, 1, "{}"])
    db.execute("DELETE FROM volumes")
    assert db.execute("SELECT * FROM release_selection_evidence").fetchall() == []


@pytest.mark.parametrize(
    "index,bad",
    [
        (1, 0),
        (1, -1),
        (2, ROOT),
        (2, DIRECT.upper()),
        (2, ROOT + ":01"),
        (2, ROOT + ":9223372036854775808"),
        (3, "usenet"),
        (4, ""),
        (4, "x" * 4097),
    ],
)
def test_identity_table_rejects_incomplete_or_invalid_tuples(db, index, bad):
    anchor = block(db)
    values = [anchor, 1, DIRECT, "torrent", "Guid"]
    values[index] = bad
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO blocklist_release_identities VALUES(?,?,?,?,?)", values)
    assert db.execute("SELECT * FROM blocklist_release_identities").fetchall() == []


def test_configuration_delete_cannot_erase_durable_identity_and_disabled_ttl_retains(
    db,
):
    identity = ReleaseIdentity(1, DIRECT, "torrent", "Guid")
    db.execute(
        "INSERT INTO indexers(id,name,type,release_source_scope) VALUES(1,'same','torznab',?)",
        (DIRECT,),
    )
    block(db, identities=[identity])
    db.execute("UPDATE blocklist SET added_at='2000-01-01'")
    db.execute("UPDATE settings SET value='0' WHERE key='blocklist_ttl_days'")
    db.execute("DELETE FROM indexers")
    assert is_blocked(db, url="rotated", identity=identity)
    db.execute("DELETE FROM blocklist")
    assert not is_blocked(db, url="rotated", identity=identity)


def test_release_identity_import_is_standalone_stdlib_only(tmp_path):
    source = Path(__file__).resolve().parents[2] / "app"
    program = (
        "import sys\nsys.path.insert(0,sys.argv[1])\nimport release_identity\n"
        "assert not {'schema','main','release_sources','fastapi'} & sys.modules.keys()\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(source)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
