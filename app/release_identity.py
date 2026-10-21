"""Immutable observed identities and durable URL-anchor persistence.

Only stdlib dependencies: offline tooling must not import the application here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
import json
import re
import sqlite3
from typing import Any, Literal, cast

Protocol = Literal["torrent", "nzb"]
Purpose = Literal["rss", "auto", "interactive"]
IndexerKind = Literal["prowlarr", "torznab", "newznab"]
_SCOPE = re.compile(r"bs1:([dp]):([0-9a-f]{64})(?::([1-9][0-9]{0,18}))?", re.ASCII)


def positive_id(value: object) -> bool:
    return type(value) is int and 0 < value <= 2**63 - 1


def parse_scope(
    value: object, *, allow_root: bool = False
) -> tuple[str, str, int | None]:
    """Validate the canonical encoding; a parent root is not release evidence."""
    match = _SCOPE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("invalid source scope")
    kind, epoch, raw_sub = match.groups()
    sub = int(raw_sub) if raw_sub is not None else None
    if (kind == "d" and sub is not None) or (sub is not None and not positive_id(sub)):
        raise ValueError("invalid source scope")
    if kind == "p" and sub is None and not allow_root:
        raise ValueError("parent root is not a release scope")
    return kind, epoch, sub


def canonical_protocol(value: object) -> Protocol | None:
    """Recognize provider evidence without the search normalizer's fallback."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized == "torrent":
        return "torrent"
    if normalized in {"nzb", "usenet"}:
        return "nzb"
    return None


def normalized_guid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    guid = value.strip()
    try:
        return guid if guid and len(guid.encode("utf-8")) <= 4096 else None
    except UnicodeError:
        return None


@dataclass(frozen=True)
class SourceContext:
    indexer_id: int
    row_scope: str
    implementation: IndexerKind
    sub_id: int | None
    protocol: Protocol
    purpose: Purpose
    fetch_series_id: int | None

    def __post_init__(self) -> None:
        if not positive_id(self.indexer_id) or (
            self.fetch_series_id is not None and not positive_id(self.fetch_series_id)
        ):
            raise ValueError("invalid source context ID")
        kind, _, encoded_sub = parse_scope(self.row_scope, allow_root=True)
        if self.protocol not in ("torrent", "nzb") or self.purpose not in (
            "rss",
            "auto",
            "interactive",
        ):
            raise ValueError("invalid source context protocol or purpose")
        if self.sub_id is not None and not positive_id(self.sub_id):
            raise ValueError("invalid source context sub-ID")
        if self.implementation == "prowlarr":
            if kind != "p" or encoded_sub is not None or self.sub_id is None:
                raise ValueError("invalid parent source context")
        elif self.implementation in ("torznab", "newznab"):
            expected = "torrent" if self.implementation == "torznab" else "nzb"
            if (
                self.protocol != expected
                or (kind == "p" and encoded_sub is None)
                or self.sub_id != encoded_sub
            ):
                raise ValueError("invalid direct source context")
        else:
            raise ValueError("invalid source implementation")

    @property
    def source_scope(self) -> str:
        if self.implementation == "prowlarr":
            return f"{self.row_scope}:{self.sub_id}"
        return self.row_scope


@dataclass(frozen=True)
class ReleaseIdentity:
    series_id: int
    source_scope: str
    protocol: Protocol
    release_guid: str

    def __post_init__(self) -> None:
        if not positive_id(self.series_id) or self.protocol not in ("torrent", "nzb"):
            raise ValueError("invalid release identity")
        parse_scope(self.source_scope)
        guid = normalized_guid(self.release_guid)
        if guid is None:
            raise ValueError("invalid release GUID")
        object.__setattr__(self, "release_guid", guid)


@dataclass(frozen=True)
class BlockEvidence:
    url: str
    identity: ReleaseIdentity

    def __post_init__(self) -> None:
        if (
            not isinstance(self.url, str)
            or not self.url
            or not isinstance(self.identity, ReleaseIdentity)
        ):
            raise ValueError("invalid block evidence")


def identity_for(item: Mapping[str, Any], series_id: int) -> ReleaseIdentity | None:
    context = item.get("_source_context")
    if not isinstance(context, SourceContext):
        return None
    protocol = canonical_protocol(item.get("protocol"))
    if protocol != context.protocol:
        return None
    guid = normalized_guid(item.get("guid"))
    if guid is None:
        return None
    try:
        return ReleaseIdentity(series_id, context.source_scope, context.protocol, guid)
    except ValueError:
        return None


def seen_identity(row: Mapping[str, Any]) -> ReleaseIdentity | None:
    """Read retained observation fields only, never current configuration."""
    protocol = canonical_protocol(row.get("protocol"))
    series_id = row.get("series_id")
    scope = row.get("release_source_scope")
    guid = row.get("release_guid")
    if (
        protocol is None
        or not isinstance(series_id, int)
        or not isinstance(scope, str)
        or not isinstance(guid, str)
    ):
        return None
    try:
        return ReleaseIdentity(series_id, scope, protocol, guid)
    except (ValueError, TypeError):
        return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate evidence field")
        result[key] = value
    return result


def encode_context(context: SourceContext) -> str:
    if not isinstance(context, SourceContext):
        raise ValueError("invalid source context")
    return json.dumps(
        {"version": 1, **asdict(context)}, sort_keys=True, separators=(",", ":")
    )


def decode_context(value: str) -> SourceContext:
    if not isinstance(value, str):
        raise ValueError("invalid source context")
    try:
        data = json.loads(value, object_pairs_hook=_unique_object)
        keys = {
            "version",
            "indexer_id",
            "row_scope",
            "implementation",
            "sub_id",
            "protocol",
            "purpose",
            "fetch_series_id",
        }
        if (
            not isinstance(data, dict)
            or set(data) != keys
            or type(data["version"]) is not int
            or data.pop("version") != 1
        ):
            raise ValueError("invalid source context")
        return SourceContext(**data)
    except (TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid source context") from exc


def read_history_evidence(
    row: Mapping[str, Any],
) -> tuple[Literal["absent", "valid", "invalid"], BlockEvidence | None]:
    raw = row.get("data")
    if raw is None or raw == "":
        return "absent", None
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(data, dict):
            return "invalid", None
        if "release_identity" not in data:
            return "absent", None
        value = data["release_identity"]
        if not isinstance(value, dict) or set(value) != {
            "version",
            "series_id",
            "url",
            "source_scope",
            "protocol",
            "release_guid",
        }:
            return "invalid", None
        if type(value["version"]) is not int or value["version"] != 1:
            return "invalid", None
        identity = ReleaseIdentity(
            value["series_id"],
            value["source_scope"],
            value["protocol"],
            value["release_guid"],
        )
        evidence = BlockEvidence(value["url"], identity)
        for outer, inner in (
            ("series_id", identity.series_id),
            ("torrent_url", evidence.url),
            ("protocol", identity.protocol),
        ):
            if (
                row.get(outer) is not None
                and row.get(outer) != ""
                and (type(row[outer]) is not type(inner) or row[outer] != inner)
            ):
                return "invalid", None
        return "valid", evidence
    except (ValueError, TypeError, KeyError, RecursionError):
        return "invalid", None


def add_block(
    db: sqlite3.Connection,
    *,
    series_id: int | None,
    url: str,
    title: str,
    reason: str,
    indexer: str | None,
    protocol: str | None,
    size_bytes: int | None,
    identities: Iterable[ReleaseIdentity] = (),
) -> int:
    """Enrich an existing anchor without changing its metadata or TTL; no commit."""
    if (
        not isinstance(url, str)
        or not url
        or (series_id is not None and not positive_id(series_id))
    ):
        raise ValueError("invalid block anchor")
    validated = tuple(identities)
    if any(not isinstance(identity, ReleaseIdentity) for identity in validated):
        raise ValueError("invalid block identity")
    db.execute(
        "INSERT INTO blocklist(series_id,torrent_url,torrent_name,reason,indexer,protocol,size_bytes) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(torrent_url) DO NOTHING",
        (series_id, url, title, reason, indexer, protocol, size_bytes),
    )
    anchor = cast(
        int,
        db.execute("SELECT id FROM blocklist WHERE torrent_url=?", (url,)).fetchone()[
            0
        ],
    )
    db.executemany(
        "INSERT INTO blocklist_release_identities(blocklist_id,series_id,source_scope,protocol,release_guid) "
        "VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
        (
            (anchor, i.series_id, i.source_scope, i.protocol, i.release_guid)
            for i in validated
        ),
    )
    return anchor


def is_blocked(
    db: sqlite3.Connection, *, url: str, identity: ReleaseIdentity | None
) -> bool:
    if db.execute("SELECT 1 FROM blocklist WHERE torrent_url=?", (url,)).fetchone():
        return True
    if not isinstance(identity, ReleaseIdentity):
        return False
    return (
        db.execute(
            "SELECT 1 FROM blocklist_release_identities AS i JOIN blocklist AS b ON b.id=i.blocklist_id "
            "WHERE i.series_id=? AND i.source_scope=? AND i.protocol=? AND i.release_guid=? LIMIT 1",
            (
                identity.series_id,
                identity.source_scope,
                identity.protocol,
                identity.release_guid,
            ),
        ).fetchone()
        is not None
    )
