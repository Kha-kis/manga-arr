"""Helpers for partial-POST-safe edit routes (Editor-form clobber epic).

The bug class: editor routes that take 15 `Form(...)` parameters with
primitive defaults and emit one wholesale `UPDATE table SET …` will
silently overwrite columns whenever a scripted client omits a field.
The HTML editor pages always send every input, so the bug only fires
from curl / HTMX-partial / scripted callers — which means the test
suite never catches it unless we explicitly write partial-POST tests.

Pattern B (the recommended fix): every edit route calls
`await request.form()`, then uses `submitted_subset` to peel off only
the fields the client actually sent. Validators run on those fields,
and the route assembles `UPDATE … SET col=?, …` with only those
columns. Columns whose form key is missing are left untouched.

Why a helper module: the alternative — adding sentinel `Form(None)`
defaults to every route — required touching every call site, every
test, and every developer to remember a fallback line on every new
field. Centralising the introspection makes the foot-gun harder to
re-introduce, and the invariant test in
`tests/python/test_editor_form_partial_invariants.py` ensures it stays
that way.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable


def submitted_subset(
    form: Any,
    fields: dict[str, tuple[str, Callable[[str], Any]]],
) -> tuple[list[str], list[Any]]:
    """Extract submitted form fields into UPDATE clause + params.

    `fields` maps form-key → (db-column-name, coerce-fn). For every
    form key present in the submitted form, the coerce function runs on
    the raw form value and the result is appended to the params list.

    Returns:
        (updates, params) — `updates` is a list of "col=?" SQL fragments
        ready to join with ", " into a SET clause; `params` is the
        matching positional values. Both are empty if no field was
        submitted, which the caller MUST treat as "skip the UPDATE
        entirely" rather than emitting a no-op `UPDATE table WHERE id=?`
        (sqlite tolerates that, but it bumps the rowversion for nothing).

    Raises:
        ValueError if a coerce function raises — the caller is expected
        to translate this into a 400 response or, more typically, to
        let it bubble (FastAPI converts to 422).
    """
    updates: list[str] = []
    params: list[Any] = []
    for form_key, (db_col, coerce) in fields.items():
        if form_key not in form:
            continue
        raw = form[form_key]
        coerced = coerce(raw)
        updates.append(f"{db_col}=?")
        params.append(coerced)
    return updates, params


# ── Common coercers ──────────────────────────────────────────────────


def str_or_none(v: Any) -> str | None:
    """Trim whitespace; treat empty string as NULL.

    Matches the pre-fix behaviour of `value if value else None` that
    most edit routes used inline."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def stripped_str(v: Any) -> str:
    """Trim whitespace, keep empty strings (for fields where empty has
    a distinct meaning from NULL — e.g. an explicit clear)."""
    return str(v or '').strip()


def int_or_none(v: Any) -> int | None:
    """Parse to int; empty / unparseable → None.

    Used for foreign-key columns like `quality_profile_id` where 0/empty
    means "unset" — historically these were stored as NULL."""
    if v in (None, '', 'null'):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def int_default_zero(v: Any) -> int:
    """Parse to int; empty / unparseable → 0.

    Used for numeric columns where 0 is a valid value (priority,
    seed_ratio, etc.) and we want to coerce raw form strings."""
    if v in (None, ''):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def float_default_zero(v: Any) -> float:
    if v in (None, ''):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def bool_int(v: Any) -> int:
    """Coerce a checkbox/select string to 0 or 1.

    HTML checkboxes submit either "1" / "on" (when checked) or are
    absent from the form entirely. Once a route calls `submitted_subset`
    the absence case is handled by the form-key check, so this only
    sees the checked case — but accepting "0" / "false" / "" as falsy
    keeps the helper robust for non-browser callers."""
    if v in (None, '', '0', 'false', 'False', 'off'):
        return 0
    return 1


def csv_to_json_array(v: Any) -> str:
    """Comma-separated input → JSON-encoded string array.

    Used for `preferred_groups`, `blocked_groups`, etc. Empty-string
    explicitly produces `[]` (the user clicked "save" with the field
    cleared) — that's distinct from the field being absent from the
    form, which is handled one level up by `submitted_subset` skipping
    the key entirely."""
    raw = str(v or '').strip()
    if not raw:
        return '[]'
    items = [s.strip() for s in raw.split(',') if s.strip()]
    return json.dumps(items)


def csv_to_int_set_str(v: Any) -> str:
    """Comma-separated ints → comma-separated cleaned ints (string).

    Used for indexer `categories` (e.g. "7000,7010,7020"). Drops
    entries that don't parse, dedupes while preserving order."""
    raw = str(v or '').strip()
    if not raw:
        return ''
    seen: set[int] = set()
    out: list[str] = []
    for tok in raw.split(','):
        tok = tok.strip()
        try:
            n = int(tok)
        except (TypeError, ValueError):
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(str(n))
    return ','.join(out)


def parsed_json_str(v: Any, default: str = '{}') -> str:
    """Validate that a string is JSON-parseable; return it as-is on
    success, or the default on parse failure.

    Used for `settings` columns (indexers, import_lists, etc.) where
    the column stores a JSON-encoded string and we want to reject
    malformed payloads rather than corrupt the row."""
    raw = str(v or '').strip()
    if not raw:
        return default
    try:
        json.loads(raw)
    except (TypeError, ValueError):
        return default
    return raw


# ── Tag-set rebuild gate ─────────────────────────────────────────────


def parsed_tag_csv(v: Any) -> list[int]:
    """Parse a CSV of tag ids; drop unparseable entries; dedupe."""
    raw = str(v or '').strip()
    if not raw:
        return []
    seen: set[int] = set()
    out: list[int] = []
    for tok in raw.split(','):
        tok = tok.strip()
        try:
            n = int(tok)
        except (TypeError, ValueError):
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


# ── Sanity for callers ───────────────────────────────────────────────


def assert_no_leftover_keys(
    form: Any,
    handled: Iterable[str],
    allowed_extra: Iterable[str] = ('csrf_token',),
) -> None:
    """Optional: raise ValueError if the form carries keys the route
    isn't aware of.

    Off by default — the dual-mode HTMX/plain-form pattern occasionally
    sends inputs the server doesn't care about (e.g. a hidden state
    marker for the JS layer). Routes that want strict-mode partial
    validation can opt in."""
    handled_set = set(handled) | set(allowed_extra)
    leftover = [k for k in form.keys() if k not in handled_set]
    if leftover:
        raise ValueError(
            f"unrecognised form fields: {sorted(leftover)}; "
            f"handled: {sorted(handled_set)}"
        )
