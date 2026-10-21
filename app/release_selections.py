"""Bounded one-use server observations. Importable by offline restore tooling."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import secrets
import sqlite3
from types import MappingProxyType
from typing import Any, Literal

from release_identity import SourceContext, decode_context, positive_id

LIFETIME = 900
ISSUER_LIMIT = 256
GLOBAL_LIMIT = 2048
SNAPSHOT_LIMIT = 16384
_SELECTION_SCHEMA_VERSION = 7
_HANDLE = re.compile(r"[A-Za-z0-9_-]{64}", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)
_PURPOSE = "blocklist-release-selection"
_BEHAVIOR = ("url", "guid", "protocol", "title", "indexer", "size_bytes", "seeders")
SelectionReason = Literal["invalid", "gone", "mismatch", "unavailable"]
_ERRORS: dict[SelectionReason, tuple[int, str]] = {
    "invalid": (400, "Invalid selection receipt"),
    "gone": (410, "Selection expired; refresh search"),
    "mismatch": (409, "Selection changed; refresh search"),
    "unavailable": (503, "Selection evidence unavailable"),
}


class SelectionError(ValueError):
    """Secretless refusal safe for an HTTP response."""

    def __init__(self, status: int, reason: SelectionReason) -> None:
        if (
            type(status) is not int
            or type(reason) is not str
            or reason not in _ERRORS
            or _ERRORS[reason][0] != status
        ):
            raise ValueError("invalid selection error")
        self.status = status
        self.reason = reason
        super().__init__(_ERRORS[reason][1])


@dataclass(frozen=True)
class SelectionIssuer:
    kind: Literal["browser", "api"]
    session_digest: str | None

    def __post_init__(self) -> None:
        if self.kind == "api" and self.session_digest is None:
            return
        if (
            self.kind == "browser"
            and type(self.session_digest) is str
            and _DIGEST.fullmatch(self.session_digest)
        ):
            return
        raise ValueError("invalid selection issuer")


def issuer_for_request(
    scope: Mapping[str, Any], cookies: Mapping[str, str]
) -> SelectionIssuer:
    """Middleware API authority wins; bypass browser state alone is not proof."""
    if scope.get("mangarr_api_key_authenticated") is True:
        return SelectionIssuer("api", None)
    from auth import AUTH_COOKIE_NAME, validate_session

    if scope.get("mangarr_browser_authenticated") is True:
        session = validate_session(cookies.get(AUTH_COOKIE_NAME, ""))
        if session is not None:
            return SelectionIssuer("browser", session["token_hash"])
    raise SelectionError(409, "mismatch")


def canonical_guid(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text(value: object, limit: int | None = None, *, nonempty: bool = False) -> bool:
    if type(value) is not str or (nonempty and not value):
        return False
    try:
        size = len(value.encode("utf-8"))
        return limit is None or size <= limit
    except UnicodeError:
        return False


def _behavior(data: Mapping[str, Any]) -> dict[str, Any]:
    if not all(key in data for key in _BEHAVIOR):
        raise ValueError("incomplete selection")
    if not (
        _text(data["url"], 8192, nonempty=True)
        and _text(data["title"], 2048)
        and _text(data["indexer"])
        and _text(data["guid"])
        and data["guid"] == canonical_guid(data["guid"])
        and type(data["protocol"]) is str
        and data["protocol"] in ("torrent", "nzb")
        and all(
            type(data[key]) is int and data[key] >= 0
            for key in ("size_bytes", "seeders")
        )
        and (
            "preferred_client_id" not in data
            or positive_id(data["preferred_client_id"])
        )
    ):
        raise ValueError("invalid selection fields")
    fields = {key: data[key] for key in _BEHAVIOR}
    if "preferred_client_id" in data:
        fields["preferred_client_id"] = data["preferred_client_id"]
    return fields


@dataclass(frozen=True)
class SelectionSnapshot:
    series_id: int
    volume_id: int
    context: SourceContext
    item: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            not positive_id(self.series_id)
            or not positive_id(self.volume_id)
            or not isinstance(self.context, SourceContext)
        ):
            raise ValueError("invalid selection target")
        if not isinstance(self.item, Mapping):
            raise ValueError("invalid selection item")
        canonical = _behavior(self.item)
        context = self.context
        if (
            context.purpose != "interactive"
            or context.fetch_series_id != self.series_id
            or type(context.protocol) is not str
            or type(context.purpose) is not str
            or type(context.implementation) is not str
            or type(context.row_scope) is not str
            or context.protocol != canonical["protocol"]
        ):
            raise ValueError("invalid selection context")
        object.__setattr__(self, "item", MappingProxyType(canonical))

    def behavior(self) -> dict[str, Any]:
        return dict(self.item)

    def observed_item(self) -> dict[str, Any]:
        return {**self.behavior(), "_source_context": self.context}


def snapshot_from_result(
    series_id: int, volume_id: int, result: Mapping[str, Any], context: object
) -> SelectionSnapshot:
    if not isinstance(context, SourceContext):
        raise ValueError("selection needs an observed source")
    return SelectionSnapshot(series_id, volume_id, context, result)


def encode_snapshot(snapshot: SelectionSnapshot) -> str:
    value = {
        "version": 1,
        "purpose": _PURPOSE,
        "series_id": snapshot.series_id,
        "volume_id": snapshot.volume_id,
        "context": {"version": 1, **asdict(snapshot.context)},
        "release": snapshot.behavior(),
    }
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if not 0 < len(raw.encode("utf-8")) <= SNAPSHOT_LIMIT:
        raise ValueError("selection snapshot too large")
    return raw


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate selection field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("invalid JSON constant")


def parse_posted_json(raw: bytes) -> Any:
    return json.loads(
        raw, object_pairs_hook=_unique_fields, parse_constant=_invalid_constant
    )


def decode_snapshot(raw: str) -> SelectionSnapshot:
    if not _text(raw, SNAPSHOT_LIMIT, nonempty=True):
        raise ValueError("invalid selection snapshot")
    value = parse_posted_json(raw.encode("utf-8"))
    if (
        not isinstance(value, dict)
        or set(value)
        != {"version", "purpose", "series_id", "volume_id", "context", "release"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["purpose"] != _PURPOSE
        or not isinstance(value["release"], dict)
        or set(value["release"]) - {*_BEHAVIOR, "preferred_client_id"}
    ):
        raise ValueError("invalid selection snapshot")
    context = decode_context(json.dumps(value["context"]))
    snapshot = snapshot_from_result(
        value["series_id"], value["volume_id"], value["release"], context
    )
    encode_snapshot(snapshot)
    return snapshot


def _digest(handle: str) -> str:
    return hashlib.sha256(handle.encode("ascii")).hexdigest()


def _prune(db: sqlite3.Connection, now: int) -> None:
    db.execute(
        "DELETE FROM release_selection_evidence WHERE expires_at<=? OR issued_at>?",
        (now, now),
    )


def _active_issuer(db: sqlite3.Connection, issuer: SelectionIssuer, now: int) -> bool:
    if issuer.kind == "api":
        return True
    # Recheck under the reserved writer so a concurrent revocation cannot race issuance.
    from auth import SESSION_IDLE_SECONDS, _parse_timestamp

    row = db.execute(
        "SELECT s.last_seen_at,s.expires_at FROM auth_sessions s JOIN auth_admin a ON a.id=s.admin_id WHERE s.token_hash=?",
        (issuer.session_digest,),
    ).fetchone()
    if row is None:
        return False
    return (
        now < _parse_timestamp(row[1]).timestamp()
        and now - _parse_timestamp(row[0]).timestamp() < SESSION_IDLE_SECONDS
    )


def _current_source(db: sqlite3.Connection, context: SourceContext) -> bool:
    from release_sources import source_context_is_current

    return source_context_is_current(db, context)


def _validate_now(now: int) -> None:
    if type(now) is not int or not 0 <= now <= 2**63 - 1 - LIFETIME:
        raise ValueError("invalid selection time")


def issue_selections(
    db: sqlite3.Connection,
    *,
    issuer: SelectionIssuer,
    snapshots: Sequence[SelectionSnapshot],
    now: int,
) -> list[str]:
    """Issue inside the caller's reserved writer; never complete its transaction."""
    _validate_now(now)
    if len(snapshots) > ISSUER_LIMIT:
        raise SelectionError(400, "invalid")
    encoded = [encode_snapshot(snapshot) for snapshot in snapshots]
    try:
        _prune(db, now)
        if not _active_issuer(db, issuer, now):
            raise SelectionError(409, "mismatch")
        if any(
            not _current_source(db, context)
            for context in {snapshot.context for snapshot in snapshots}
        ):
            raise SelectionError(409, "mismatch")
        reserve = len(snapshots)
        db.execute(
            "DELETE FROM release_selection_evidence WHERE handle_digest IN ("
            "SELECT handle_digest FROM release_selection_evidence WHERE issuer_kind=? AND session_digest IS ? "
            "ORDER BY issued_at DESC,handle_digest DESC LIMIT -1 OFFSET ?)",
            (issuer.kind, issuer.session_digest, ISSUER_LIMIT - reserve),
        )
        db.execute(
            "DELETE FROM release_selection_evidence WHERE handle_digest IN ("
            "SELECT handle_digest FROM release_selection_evidence ORDER BY issued_at DESC,handle_digest DESC LIMIT -1 OFFSET ?)",
            (GLOBAL_LIMIT - reserve,),
        )
        handles = [secrets.token_urlsafe(48) for _ in snapshots]
        db.executemany(
            "INSERT INTO release_selection_evidence(handle_digest,issuer_kind,session_digest,issued_at,expires_at,series_id,volume_id,snapshot_json) VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    _digest(handle),
                    issuer.kind,
                    issuer.session_digest,
                    now,
                    now + LIFETIME,
                    snapshot.series_id,
                    snapshot.volume_id,
                    raw,
                )
                for handle, snapshot, raw in zip(
                    handles, snapshots, encoded, strict=True
                )
            ],
        )
        return handles
    except SelectionError:
        raise
    except (sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
        raise SelectionError(503, "unavailable") from exc


def consume_selection(
    db: sqlite3.Connection,
    *,
    handle: object,
    issuer: SelectionIssuer,
    series_id: int,
    volume_id: int,
    posted: Mapping[str, Any],
    now: int,
) -> SelectionSnapshot:
    """Validate/delete once in the caller's writer; caller owns commit/rollback."""
    _validate_now(now)
    if type(handle) is not str or _HANDLE.fullmatch(handle) is None:
        raise SelectionError(400, "invalid")
    try:
        _prune(db, now)
        row = db.execute(
            "SELECT issuer_kind,session_digest,series_id,volume_id,snapshot_json FROM release_selection_evidence WHERE handle_digest=?",
            (_digest(handle),),
        ).fetchone()
        if row is None:
            raise SelectionError(410, "gone")
        if (row[0], row[1]) != (
            issuer.kind,
            issuer.session_digest,
        ) or not _active_issuer(db, issuer, now):
            raise SelectionError(409, "mismatch")
        try:
            snapshot = decode_snapshot(row[4])
        except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
            raise SelectionError(503, "unavailable") from exc
        if (row[2], row[3]) != (snapshot.series_id, snapshot.volume_id):
            raise SelectionError(503, "unavailable")
        if (series_id, volume_id) != (snapshot.series_id, snapshot.volume_id):
            raise SelectionError(409, "mismatch")
        try:
            behavior = _behavior(posted)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise SelectionError(409, "mismatch") from exc
        if behavior != snapshot.behavior():
            raise SelectionError(409, "mismatch")
        if not db.execute(
            "SELECT 1 FROM volumes WHERE id=? AND series_id=?", (volume_id, series_id)
        ).fetchone() or not _current_source(db, snapshot.context):
            raise SelectionError(409, "mismatch")
        db.execute(
            "DELETE FROM release_selection_evidence WHERE handle_digest=?",
            (_digest(handle),),
        )
        return snapshot
    except SelectionError:
        raise
    except (sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
        raise SelectionError(503, "unavailable") from exc


def clear_selection_evidence(
    db: sqlite3.Connection, *, allow_legacy: bool = False
) -> None:
    """Clear ephemeral capabilities only; caller owns the restore/startup transaction."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > _SELECTION_SCHEMA_VERSION:
        raise sqlite3.DatabaseError("unsupported selection evidence schema")
    exists = db.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='release_selection_evidence'"
    ).fetchone()
    if not exists:
        if allow_legacy and version < _SELECTION_SCHEMA_VERSION:
            return
        raise sqlite3.DatabaseError("selection evidence table unavailable")
    db.execute("DELETE FROM release_selection_evidence")
