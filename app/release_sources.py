"""Transaction-local configured source bindings and immutable request snapshots."""

from __future__ import annotations

from collections.abc import Mapping
import secrets
import sqlite3
from typing import Any, cast

from release_identity import (
    IndexerKind,
    Purpose,
    SourceContext,
    canonical_protocol,
    parse_scope,
    positive_id,
)


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
    from routers.indexers import _active_indexer_rows

    if not isinstance(context, SourceContext):
        return False
    for candidate in _active_indexer_rows(db, context.purpose, context.fetch_series_id):
        row = dict(candidate)
        if row["id"] == context.indexer_id:
            return (
                row["type"] == context.implementation
                and row.get("release_source_scope") == context.row_scope
            )
    return False
