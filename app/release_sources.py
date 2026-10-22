"""Transaction-local configured source bindings and immutable request snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import json
import secrets
import sqlite3
from typing import Any, cast

from release_identity import (
    IndexerKind,
    Purpose,
    SourceContext,
    canonical_protocol,
    decode_context,
    encode_context,
    parse_scope,
    positive_id,
)


@dataclass(frozen=True)
class SourceRequest:
    """Request freshness only, including when protocol cannot certify identity."""

    indexer_id: int
    row_scope: str
    implementation: IndexerKind
    sub_id: int | None
    purpose: Purpose
    fetch_series_id: int | None

    def __post_init__(self) -> None:
        if not positive_id(self.indexer_id) or (
            self.fetch_series_id is not None and not positive_id(self.fetch_series_id)
        ):
            raise ValueError("invalid request source ID")
        if type(self.purpose) is not str or self.purpose not in (
            "rss",
            "auto",
            "interactive",
        ):
            raise ValueError("invalid request source purpose")
        kind, _, encoded_sub = parse_scope(self.row_scope, allow_root=True)
        if self.sub_id is not None and not positive_id(self.sub_id):
            raise ValueError("invalid request source sub-ID")
        if type(self.implementation) is not str:
            raise ValueError("invalid request source implementation")
        if self.implementation == "prowlarr":
            if kind != "p" or encoded_sub is not None:
                raise ValueError("invalid request parent source")
        elif self.implementation in ("torznab", "newznab"):
            if (kind == "p" and encoded_sub is None) or self.sub_id != encoded_sub:
                raise ValueError("invalid request direct source")
        else:
            raise ValueError("invalid request source implementation")


def capture_source_request(
    row: Mapping[str, Any], *, purpose: Purpose, fetch_series_id: int | None
) -> SourceRequest | None:
    """Capture the local row before I/O without inventing protocol evidence."""
    try:
        _, _, sub_id = parse_scope(row.get("release_source_scope"), allow_root=True)
        return SourceRequest(
            row["id"],
            row["release_source_scope"],
            row["type"],
            sub_id,
            purpose,
            fetch_series_id,
        )
    except (ValueError, KeyError, TypeError):
        return None


def observe_source_items(
    items: list[dict[str, Any]],
    request: SourceRequest | None,
    *,
    expected_protocol: object,
) -> list[dict[str, Any]]:
    """Attach server runtime values; permissive search normalization is not proof."""
    expected = canonical_protocol(expected_protocol)
    for item in items:
        item["_source_request"] = request
        item.pop("_source_context", None)
        observed = canonical_protocol(item.pop("_protocol_evidence", None))
        if request is not None and expected is not None and observed == expected:
            try:
                item["_source_context"] = SourceContext(
                    **asdict(request), protocol=expected
                )
            except (ValueError, TypeError):
                pass
    return items


def _request_from_context(context: SourceContext) -> SourceRequest:
    values = asdict(context)
    values.pop("protocol")
    return SourceRequest(**values)


def item_source_is_current(db: sqlite3.Connection, item: Mapping[str, Any]) -> bool:
    """Absent legacy input is URL-only; failed server capture is not absence."""
    context = item.get("_source_context")
    if "_source_request" in item:
        request = item["_source_request"]
        if not isinstance(request, SourceRequest) or not _source_is_current(
            db, request
        ):
            return False
        if (
            isinstance(context, SourceContext)
            and _request_from_context(context) != request
        ):
            return False
    if isinstance(context, SourceContext):
        return canonical_protocol(item.get("protocol")) == context.protocol and (
            "_source_request" in item or source_context_is_current(db, context)
        )
    return True


def pending_source_fields(
    item: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Retain v1 identity context or a distinct freshness-only envelope."""
    context = item.get("_source_context")
    raw_guid = item.get("guid")
    guid = (raw_guid.strip() or None) if isinstance(raw_guid, str) else None
    if isinstance(context, SourceContext):
        return context.source_scope, guid, encode_context(context)
    if "_source_request" not in item:
        return None, None, None
    request = item["_source_request"]
    value = {
        "version": 1,
        "kind": "blocklist-request-freshness",
        "request": asdict(request) if isinstance(request, SourceRequest) else None,
        "protocol": item.get("protocol"),
    }
    return None, guid, json.dumps(value, sort_keys=True, separators=(",", ":"))


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate request evidence field")
        result[key] = value
    return result


def restore_pending_source(row: Mapping[str, Any], item: dict[str, Any]) -> None:
    """Decode only retained server evidence, never join mutable configuration."""
    raw = row.get("release_context")
    scope = row.get("release_source_scope")
    guid = row.get("release_guid")
    if raw is None and scope is None and guid is None:
        return
    if (
        not isinstance(raw, str)
        or not raw
        or (
            guid is not None
            and (not isinstance(guid, str) or not guid or guid != guid.strip())
        )
    ):
        raise ValueError("invalid pending source evidence")
    try:
        context = decode_context(raw)
    except (ValueError, TypeError, RecursionError):
        try:
            value = json.loads(raw, object_pairs_hook=_unique_fields)
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "kind", "request", "protocol"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or value["kind"] != "blocklist-request-freshness"
                or type(value["protocol"]) is not str
                or value["protocol"] not in ("torrent", "nzb")
                or value["protocol"] != row.get("protocol")
                or scope is not None
                or not isinstance(value["request"], dict)
            ):
                raise ValueError("invalid pending request evidence")
            request = SourceRequest(**value["request"])
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise ValueError("invalid pending source evidence") from exc
    else:
        if scope != context.source_scope or row.get("protocol") != context.protocol:
            raise ValueError("inconsistent pending source evidence")
        request = _request_from_context(context)
        item["_source_context"] = context
    if request.implementation == "prowlarr" and request.sub_id is None:
        raise ValueError("pending request requires a concrete sub-source")
    if (
        request.fetch_series_id is not None
        and request.fetch_series_id != row["series_id"]
    ):
        raise ValueError("inconsistent pending source series")
    item["_source_request"] = request
    item["guid"] = guid or ""


def effective_endpoint(row: Mapping[str, Any]) -> str:
    return (row.get("url") or "").rstrip("/")


def facade_matches(
    row: Mapping[str, Any], parent: Mapping[str, Any], sub_id: object
) -> bool:
    return (
        parent.get("type") == "prowlarr"
        and row.get("type") in ("torznab", "newznab")
        and positive_id(sub_id)
        and positive_id(parent.get("id"))
        and row.get("parent_prowlarr_id") == parent["id"]
        and row.get("prowlarr_indexer_id") == sub_id
        and bool(effective_endpoint(parent))
        and effective_endpoint(row) == f"{effective_endpoint(parent)}/{sub_id}"
    )


def bind_indexer_scope(
    db: sqlite3.Connection,
    *,
    indexer_id: int,
    previous: Mapping[str, Any] | None,
    verified_parent: Mapping[str, Any] | None = None,
    sub_id: int | None = None,
) -> str:
    """Bind inside the caller's configuration transaction, never across HTTP I/O."""
    found = db.execute("SELECT * FROM indexers WHERE id=?", (indexer_id,)).fetchone()
    if found is None:
        raise ValueError("source configuration unavailable")
    row = dict(found)
    scope: str | None = None
    if verified_parent is not None:
        if not facade_matches(row, verified_parent, sub_id):
            raise ValueError("source configuration mismatch")
        root = verified_parent.get("release_source_scope")
        kind, _, child = parse_scope(root, allow_root=True)
        if kind != "p" or child is not None:
            raise ValueError("invalid verified parent")
        scope = f"{root}:{sub_id}"
    elif previous is not None:
        old = dict(previous)
        unchanged = all(
            row.get(key) == old.get(key)
            for key in ("type", "parent_prowlarr_id", "prowlarr_indexer_id")
        )
        unchanged = unchanged and effective_endpoint(row) == effective_endpoint(old)
        if unchanged and old.get("release_source_scope") is not None:
            try:
                kind, _, child = parse_scope(
                    old["release_source_scope"], allow_root=True
                )
                if (row["type"] == "prowlarr") == (kind == "p" and child is None):
                    scope = old["release_source_scope"]
            except ValueError:
                scope = None
    if scope is None:
        kind = "p" if row["type"] == "prowlarr" else "d"
        scope = f"bs1:{kind}:{secrets.token_hex(32)}"
    db.execute(
        "UPDATE indexers SET release_source_scope=? WHERE id=?", (scope, indexer_id)
    )
    return scope


def capture_source_context(
    row: Mapping[str, Any],
    *,
    purpose: Purpose,
    fetch_series_id: int | None,
    sub_id: int | None = None,
    observed_protocol: object = None,
) -> SourceContext | None:
    row = dict(row)
    implementation = row.get("type")
    if not isinstance(implementation, str):
        return None
    known = {"torznab": "torrent", "newznab": "nzb"}.get(implementation)
    protocol = (
        canonical_protocol(observed_protocol)
        if observed_protocol is not None
        else canonical_protocol(known)
    )
    if protocol is None or (known is not None and known != protocol):
        return None
    try:
        _, _, encoded_sub = parse_scope(
            row.get("release_source_scope"), allow_root=True
        )
        if sub_id is None and implementation != "prowlarr":
            sub_id = encoded_sub
        return SourceContext(
            row["id"],
            row["release_source_scope"],
            cast(IndexerKind, implementation),
            sub_id,
            protocol,
            purpose,
            fetch_series_id,
        )
    except (ValueError, KeyError, TypeError):
        return None


def source_context_is_current(db: sqlite3.Connection, context: SourceContext) -> bool:
    return isinstance(context, SourceContext) and _source_is_current(db, context)


def _source_is_current(
    db: sqlite3.Connection, context: SourceContext | SourceRequest
) -> bool:
    from routers.indexers import _active_indexer_rows

    if context.implementation == "prowlarr" and context.sub_id is None:
        return False
    for candidate in _active_indexer_rows(db, context.purpose, context.fetch_series_id):
        row = dict(candidate)
        if row["id"] == context.indexer_id:
            return (
                row["type"] == context.implementation
                and row.get("release_source_scope") == context.row_scope
            )
    return False
