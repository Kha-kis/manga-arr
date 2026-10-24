"""Fresh-process controls for the claim/secret lower-layer import boundary."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
from typing import cast

import pytest

from test_file_identity_schema import ledger_db as ledger_db, ledger_path as ledger_path
from download_identity import DownloadIdentity
from test_file_identity_observation import (
    ProducerMedia,
    producer_media as producer_media,
)

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def test_pack_marker_formatter_retains_cleanup_alias() -> None:
    import import_pack_cleanup
    import private_pack_claim

    assert (
        import_pack_cleanup._pack_owner_marker_payload
        is private_pack_claim._pack_owner_marker_payload
    )


@pytest.mark.parametrize(
    "identity,token,digest",
    [
        (
            DownloadIdentity(1, "torrent", " AbC "),
            "opaque-owner",
            "53341fe7e963e6a101e4baafaed6221fc84b65f9969d32155dcb11232b1c8be6",
        ),
        (
            DownloadIdentity(2, "nzb", " Case-ID "),
            "nzb-token",
            "3b2339e5a525697b8f183e596e1e8e54bd63e73078cb1175d1f5bb59e1bcf306",
        ),
        (
            DownloadIdentity(None, None, " Mixed "),
            "owner-\u00e9",
            "745d80e895d0dbffd55d229ddee3507859bb907831076aa29ed836a055ab03cb",
        ),
        (
            DownloadIdentity(1, "torrent", "AbC"),
            "x\x00y",
            "35b0f304f3176e64e67665b7c3c95dfc6a9c338916644260345c2321927af3fc",
        ),
    ],
)
def test_pack_marker_independent_legacy_byte_vectors(
    identity: DownloadIdentity, token: str, digest: str
) -> None:
    from import_pack_cleanup import _pack_owner_marker_payload

    expected = b"mangarr-pack-owner-v1:" + digest.encode("ascii") + b"\n"
    assert _pack_owner_marker_payload(identity, token) == expected


@pytest.mark.parametrize(
    "identity,token,message",
    [
        (DownloadIdentity(1, "torrent", "abc"), "", "owner_token must be non-empty"),
        (DownloadIdentity(1, "torrent", ""), "", "owner_token must be non-empty"),
        (
            DownloadIdentity(1, "torrent", " \t "),
            "owner",
            "download identity must be non-empty",
        ),
        (
            DownloadIdentity(2, "nzb", ""),
            "owner",
            "download identity must be non-empty",
        ),
    ],
)
def test_pack_marker_invalid_inputs_preserve_exact_errors(
    identity: DownloadIdentity, token: str, message: str
) -> None:
    from import_pack_cleanup import _pack_owner_marker_payload

    with pytest.raises(ValueError) as error:
        _pack_owner_marker_payload(identity, token)
    assert str(error.value) == message


@pytest.mark.parametrize("reserialized", [False, True])
def test_frozen_transferred_claim_relationship_has_no_cleanup_dependency(
    producer_media: ProducerMedia,
    reserialized: bool,
) -> None:
    from contextlib import closing
    import file_identity_consumers as consumers
    from private_pack_claim import PackDirectoryClaim
    from test_file_identity_observation import (
        captured_fixture,
        media_snapshot,
        producer_pack,
    )

    env = producer_media
    producer_pack(env, "attached", tombstone=True, nested=True)
    if reserialized:
        with closing(sqlite3.connect(env.db_path)) as db, db:
            raw = db.execute(
                "SELECT carrier_json FROM import_pack_cleanup_tombstones"
            ).fetchone()[0]
            db.execute(
                "UPDATE import_pack_cleanup_tombstones SET carrier_json=?",
                (json.dumps(json.loads(raw), indent=2),),
            )
    closure, before_rows = captured_fixture(env)
    before_media = media_snapshot(env.scope)
    snapshot = next(s for s in closure.snapshots if s.owner.domain == "pack_tombstone")
    raw_claim = cast(str, json.loads(snapshot.rows_json)["root"]["row"]["carrier_json"])
    claim = PackDirectoryClaim.from_json(raw_claim)
    assert (raw_claim != claim.to_json()) == reserialized
    references = consumers._transferred_claim_restore_references(
        closure, snapshot.owner, claim
    )
    expected = json.dumps(
        [(asdict(spec), relationship) for spec, relationship in references],
        sort_keys=True,
    )
    payload = json.dumps(
        {
            "parent": str(env.scope),
            "rows": [asdict(r) for r in closure.raw_rows],
            "owner_key": snapshot.owner.key,
            "claim": raw_claim,
            "snapshots": [s.rows_json for s in closure.snapshots],
            "references": expected,
        }
    )
    script = r"""
import builtins
from dataclasses import asdict
import json
from pathlib import Path
import sys

assert sys.flags.isolated
assert sys.dont_write_bytecode, f"child dont_write_bytecode={sys.dont_write_bytecode}"
sys.path.insert(0, sys.argv[1])
original_import = builtins.__import__
def bounded_import(name, *args, **kwargs):
    assert name not in {"import_pack_cleanup", "import_queue", "rescan", "main"}, "transferred reference imported cleanup/bootstrap"
    assert not name.startswith("routers"), "transferred reference imported routers"
    return original_import(name, *args, **kwargs)
builtins.__import__ = bounded_import
import file_identity_consumers as consumers
from file_identity import OwnerRef
from private_pack_claim import PackDirectoryClaim
for name in ("file_identity_consumers", "file_identity", "private_pack_claim"):
    assert Path(sys.modules[name].__file__).resolve() == Path(sys.argv[1]) / (name + ".py")
payload = json.loads(sys.stdin.read())
raw = {name: [] for name in consumers._TABLES}
for row in payload["rows"]:
    raw[row["table"]].append(consumers.RawRow(row["table"], tuple(row["key"]), tuple(tuple(v) for v in row["values"])))
closure = consumers._bind_references(raw, payload["parent"], consumers.InventoryBudget())
assert [s.rows_json for s in closure.snapshots] == payload["snapshots"]
claim = PackDirectoryClaim.from_json(payload["claim"])
owner = OwnerRef("pack_tombstone", tuple(payload["owner_key"]))
references = consumers._transferred_claim_restore_references(closure, owner, claim)
assert json.dumps([(asdict(spec), relationship) for spec, relationship in references], sort_keys=True) == payload["references"]
assert "import_pack_cleanup" not in sys.modules and "import_queue" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(_APP_ROOT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after, after_rows = captured_fixture(env)
    assert after == closure and after_rows == before_rows
    assert media_snapshot(env.scope) == before_media


_EXPECTED_ROW_FIELDS = (
    "id",
    "series_id",
    "status",
    "grabbed_at",
    "imported_at",
    "import_path",
    "quality",
    "size_bytes",
    "download_id",
    "download_client_id",
    "protocol",
    "torrent_name",
    "indexer",
    "client",
    "release_group",
)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("_PUBLICATION_ROW_FIELDS", _EXPECTED_ROW_FIELDS),
        (
            "_PUBLICATION_VOLUME_FIELDS",
            _EXPECTED_ROW_FIELDS
            + (
                "volume_num",
                "chapter_num",
                "is_special",
                "pack_type",
                "vol_range_start",
                "vol_range_end",
                "edition_type",
                "language",
                "source_url",
            ),
        ),
        (
            "_PUBLICATION_CHAPTER_FIELDS",
            _EXPECTED_ROW_FIELDS
            + (
                "volume_id",
                "chapter_num",
                "chapter_range_end",
                "torrent_url",
            ),
        ),
    ],
)
def test_publication_field_tuple_alias_values_and_order(
    name: str, expected: tuple[str, ...]
) -> None:
    import import_kinds
    import import_plan

    owned = getattr(import_kinds, name)
    assert type(owned) is tuple
    assert owned == expected
    assert getattr(import_plan, name) is owned


def test_isolated_publication_snapshot_has_no_planner_dependency(
    ledger_db: sqlite3.Connection,
) -> None:
    from file_identity import OwnerRef
    from file_identity_consumers import roles_for
    from file_identity_store import capture_owner_snapshot
    from test_file_identity_inventory import (
        all_rows,
        context,
        encoded,
        producer_admission,
        publication,
        queue,
        rows,
    )

    context(ledger_db)
    queue(ledger_db)
    publication(ledger_db)
    frozen = json.loads(
        rows(ledger_db, "import_publications")[0]["queue_snapshot_json"]
    )
    frozen["_file_publication"]["admission"] = producer_admission(ledger_db, frozen)
    proof_json = encoded(frozen)
    ledger_db.execute(
        "UPDATE import_publications SET queue_snapshot_json=?", (proof_json,)
    )
    ledger_db.commit()
    before = all_rows(ledger_db)
    snapshot = capture_owner_snapshot(ledger_db, OwnerRef("publication", (1,)))
    expected_roles = json.dumps(
        [asdict(role) for role in roles_for(snapshot)], sort_keys=True
    )
    payload = json.dumps(
        {
            "snapshot_json": snapshot.rows_json,
            "sha256": snapshot.sha256,
            "roles_json": expected_roles,
        }
    )
    script = r"""
import builtins
from dataclasses import asdict
import json
from pathlib import Path
import sys

assert sys.flags.isolated
assert sys.dont_write_bytecode, f"child dont_write_bytecode={sys.dont_write_bytecode}"
sys.path.insert(0, sys.argv[1])
original_import = builtins.__import__
def bounded_import(name, *args, **kwargs):
    assert name not in {"main", "import_plan", "rescan", "import_commit"}, "snapshot inventory imported planner/bootstrap"
    assert not name.startswith("routers"), "snapshot inventory imported routers"
    return original_import(name, *args, **kwargs)
builtins.__import__ = bounded_import
from file_identity import OwnerRef
from file_identity_consumers import roles_for
from file_identity_store import OwnerSnapshot
for name in ("file_identity", "file_identity_consumers", "file_identity_store"):
    assert Path(sys.modules[name].__file__).resolve() == Path(sys.argv[1]) / (name + ".py")
payload = json.loads(sys.stdin.read())
snapshot = OwnerSnapshot(OwnerRef("publication", (1,)), payload["snapshot_json"], payload["sha256"])
roles = roles_for(snapshot)
assert json.dumps([asdict(role) for role in roles], sort_keys=True) == payload["roles_json"]
assert snapshot.rows_json == payload["snapshot_json"]
assert "import_plan" not in sys.modules and "main" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(_APP_ROOT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert capture_owner_snapshot(ledger_db, snapshot.owner) == snapshot
    assert (
        ledger_db.execute(
            "SELECT queue_snapshot_json FROM import_publications"
        ).fetchone()[0]
        == proof_json
    )
    assert all_rows(ledger_db) == before


@pytest.mark.parametrize("order", ["shared", "claims_first", "config_first"])
def test_secret_health_never_bootstraps_application(tmp_path: Path, order: str) -> None:
    script = r'''
import builtins
import io
import logging
from pathlib import Path
import sqlite3
import sys
import socket

assert sys.flags.isolated
assert sys.dont_write_bytecode, f"child dont_write_bytecode={sys.dont_write_bytecode}"
sys.path.insert(0, sys.argv[1])
def no_network(*args, **kwargs):
    raise AssertionError("standalone health attempted network IO")
socket.socket.connect = no_network
socket.getaddrinfo = no_network

original_import = builtins.__import__
def bounded_import(name, *args, **kwargs):
    assert name != "main" and not name.startswith("routers"), "health imported application bootstrap"
    return original_import(name, *args, **kwargs)
builtins.__import__ = bounded_import
if sys.argv[2] == "claims_first":
    import file_identity_claims
    import config
elif sys.argv[2] == "config_first":
    import config
    import file_identity_claims
import shared
import security

for name in ("shared", "security", "config", "file_identity_claims"):
    if name in sys.modules:
        assert Path(sys.modules[name].__file__).resolve() == Path(sys.argv[1]) / (name + ".py")
assert security._SECRET_CIPHER is None
output = io.StringIO()
handler = logging.StreamHandler(output)
logging.getLogger().addHandler(handler)
with sqlite3.connect(":memory:") as db:
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE settings(key TEXT, value TEXT);
        CREATE TABLE indexers(name TEXT, api_key TEXT);
        CREATE TABLE download_clients(name TEXT, password TEXT);
        CREATE TABLE notification_connections(name TEXT, type TEXT, settings TEXT);
        INSERT INTO settings VALUES('api_key', 'enc:v1:CANARY-CIPHERTEXT');
        INSERT INTO settings VALUES('komga_pass', 'CANARY-PLAINTEXT');
    """)
    changes = db.total_changes
    summary = shared.get_secret_health_summary(db)
    assert summary == {"has_warning": True, "encrypted_present": True,
        "affected_count": 1, "affected_items": ["Setting: api_key"]}
    assert db.total_changes == changes
assert "CANARY" not in output.getvalue()
assert security._SECRET_CIPHER is None
assert "main" not in sys.modules
assert not any(name.startswith("routers.") for name in sys.modules)
assert not list(Path(sys.argv[3]).iterdir())
'''
    env = os.environ.copy()
    env["MANGARR_CONFIG_DIR"] = str(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            script,
            str(_APP_ROOT),
            order,
            str(tmp_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_secret_declarations_preserve_exact_public_aliases() -> None:
    import config
    import main
    import security

    assert config.SETTINGS_SECRET_KEYS is security.SETTINGS_SECRET_KEYS
    assert main.SETTINGS_SECRET_KEYS is security.SETTINGS_SECRET_KEYS
    assert isinstance(security.SETTINGS_SECRET_KEYS, frozenset)
    assert security.SETTINGS_SECRET_KEYS == frozenset(
        {"api_key", "komga_user", "komga_pass", "google_books_api_key"}
    )
    assert (
        config.NOTIFICATION_SECRET_KEYS_BY_TYPE
        is security.NOTIFICATION_SECRET_KEYS_BY_TYPE
    )
    assert (
        main.NOTIFICATION_SECRET_KEYS_BY_TYPE
        is security.NOTIFICATION_SECRET_KEYS_BY_TYPE
    )
    assert security.NOTIFICATION_SECRET_KEYS_BY_TYPE == {
        "discord": ("webhook_url",),
        "slack": ("webhook_url",),
        "telegram": ("bot_token",),
        "ntfy": ("token",),
        "gotify": ("app_token",),
        "pushover": ("user_key", "api_token"),
        "webhook": ("url",),
        "email": ("password",),
        "apprise": ("url", "config_key"),
        "pushbullet": ("access_token",),
    }
