"""Architecture-invariant tripwire for the editor-form clobber epic.

These aren't behaviour tests — they're shape constraints on every
`@router.post` edit handler in `app/routers/`. Without them, a future
PR that re-introduces the wholesale-UPDATE pattern would slip through
review unless someone happened to look for it.

The two invariants:

  (a) NO_JSON_ARRAY_DEFAULT
      No edit-route Form parameter has a default of "[]", "{}", or any
      string starting with "[" — those defaults silently empty arrays
      when the form omits the field. Use sentinel-None / form-key
      introspection instead.

  (b) NO_WHOLESALE_UPDATE_WITHOUT_INTROSPECTION
      No edit-route handler emits an `UPDATE … SET col1=?, col2=?, …`
      with two or more "=?" placeholders unless its body also references
      `request.form()` or `request.json()` (i.e. it knows which fields
      the client actually sent).

`_KNOWN_CLOBBER_ROUTES` lists every route currently flagged by the
audit. As each PR converts a route, its entry is removed from the
allowlist. PR-D removes the last entries and removes the xfail flag on
`test_zero_unconverted_clobber_routes_remain` — at which point the
tripwire is hot and any regression fails CI.

The audit table (route file:line → category) is in the docstring of
each test — see in-line.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROUTERS_DIR = pathlib.Path(__file__).resolve().parents[2] / "app" / "routers"

# ── Allowlist of routes still pending conversion ─────────────────────
# Format: (file_basename, route_method, route_path)
# Removed entry-by-entry as the conversion PRs land.
_KNOWN_CLOBBER_ROUTES: set[tuple[str, str, str]] = {
    # CRITICAL — PR-B (CONVERTED)
    # HIGH      — PR-C (CONVERTED)
    # HIGH/MEDIUM — PR-D
    ('notification_connections.py', 'POST', '/notifications/{conn_id}'),
    ('settings_.py',              'POST', '/settings/general'),
}

# The audit also flagged `POST /settings` itself, but that route already
# implements per-field `if v or k in _CLEARABLE_KEYS` — the closest
# existing example of the right pattern. It's excluded from the
# allowlist because it doesn't fit the multi-column-UPDATE shape (it
# writes a single `settings` key/value table).

# Forbidden default literals for Pattern C (JSON-array-string defaults).
_FORBIDDEN_DEFAULTS = {
    '[]',  # JSON empty array — clobbers list-valued columns
    '{}',  # JSON empty object — clobbers settings-blob columns
}


def _iter_post_routes():
    """Yield (file_path, func_def, route_path) for every @router.post
    handler in app/routers/. The route path is recovered from the
    decorator's first positional arg."""
    for py in sorted(ROUTERS_DIR.glob('*.py')):
        if py.name.startswith('_'):
            continue
        try:
            tree = ast.parse(py.read_text(), filename=str(py))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                # Match @router.post(path, ...) — function-call decorator
                if not isinstance(deco, ast.Call):
                    continue
                attr = deco.func
                if not (isinstance(attr, ast.Attribute) and attr.attr == 'post'):
                    continue
                if not deco.args:
                    continue
                first = deco.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    yield py, node, first.value


def _is_edit_route(path: str, func_name: str) -> bool:
    """Edit routes are POST handlers that update an existing resource.

    Three signals (any one is enough):
      1. Path ends in `/{xxx_id}` (canonical edit-by-id route)
      2. Path ends in `/{xxx_id}/edit` (legacy form action)
      3. Function name starts with `edit_`, `update_`, or `save_`

    This excludes create routes (no path param + name like `create_*`)
    and action endpoints (path ends in `/delete`, `/toggle`, etc., and
    the function names match those actions)."""
    if re.search(r'/\{[a-z_]+_id\}$', path):
        return True
    if re.search(r'/\{[a-z_]+_id\}/edit$', path):
        return True
    if func_name.startswith(('edit_', 'update_', 'save_')):
        return True
    return False


def _function_source(func: ast.AST, source_text: str) -> str:
    """Best-effort source slice for a function body — used by the
    SQL-grep invariant. Falls back to ast.unparse if ranges aren't
    available."""
    if hasattr(func, 'lineno') and hasattr(func, 'end_lineno'):
        lines = source_text.splitlines()
        return '\n'.join(lines[func.lineno - 1:func.end_lineno])
    return ast.unparse(func)


# ── Invariant (a) ────────────────────────────────────────────────────


def test_no_edit_route_uses_json_array_string_default():
    """Pattern C tripwire: a Form param defaulting to '[]', '{}', or
    similar literal will silently empty the column whenever the client
    omits the field.

    For every edit route NOT on the allowlist, walk its arguments and
    fail if any default value matches the forbidden set.

    Allowlisted routes are *expected* to still violate this — they're
    the routes the conversion PRs will fix. A regression elsewhere will
    fail this test.
    """
    violations: list[str] = []
    for py, func, path in _iter_post_routes():
        if not _is_edit_route(path, func.name):
            continue
        key = (py.name, 'POST', path)
        if key in _KNOWN_CLOBBER_ROUTES:
            continue
        defaults = func.args.defaults + func.args.kw_defaults
        for d in defaults:
            if d is None:
                continue
            # Form(default_value) → ast.Call
            if isinstance(d, ast.Call) and getattr(d.func, 'id', None) == 'Form':
                if d.args and isinstance(d.args[0], ast.Constant):
                    val = d.args[0].value
                    if isinstance(val, str) and (
                        val in _FORBIDDEN_DEFAULTS or val.startswith('[')
                    ):
                        violations.append(
                            f"{py.name}:{func.lineno} {func.name} "
                            f"({path}): Form default = {val!r}"
                        )
    assert not violations, (
        "Edit routes must not use JSON-array-string Form defaults — "
        "missing form fields silently wipe array/object columns. "
        "Convert to request.form() introspection (see "
        "app/routers/_form_helpers.py).\n  " + "\n  ".join(violations)
    )


# ── Invariant (b) ────────────────────────────────────────────────────


_UPDATE_RE = re.compile(
    r"UPDATE\s+\w+\s+SET\s+([^;)]+?)\s+WHERE",
    re.IGNORECASE | re.DOTALL,
)


def _placeholder_count(set_clause: str) -> int:
    """How many '=?' placeholders are in this SET clause?"""
    return len(re.findall(r"=\s*\?", set_clause))


def test_no_edit_route_emits_multi_column_update_without_form_introspection():
    """Pattern A tripwire: an edit route that emits
    `UPDATE table SET a=?, b=?, c=? WHERE id=?` without ever calling
    `await request.form()` or `await request.json()` is by definition
    a wholesale-overwrite route — it can't possibly know which columns
    the client actually wanted to change.

    For every edit route NOT on the allowlist, scan its source for
    UPDATE statements with ≥2 placeholders, and fail if found unless
    the function body also references one of:
      - request.form()
      - request.json()
      - body.get(...)         (JSON-PATCH style, e.g. patch_series)
      - submitted_subset(...)  (the helper from _form_helpers.py)
    """
    violations: list[str] = []
    for py, func, path in _iter_post_routes():
        if not _is_edit_route(path, func.name):
            continue
        key = (py.name, 'POST', path)
        if key in _KNOWN_CLOBBER_ROUTES:
            continue
        body_src = _function_source(func, py.read_text())
        # Skip if no multi-column UPDATE at all
        multi_col = False
        for m in _UPDATE_RE.finditer(body_src):
            if _placeholder_count(m.group(1)) >= 2:
                multi_col = True
                break
        if not multi_col:
            continue
        # Multi-column UPDATE present — must be paired with introspection
        introspects = (
            'request.form()' in body_src
            or 'request.json()' in body_src
            or 'submitted_subset' in body_src
            or 'body.get(' in body_src   # JSON-PATCH style
        )
        if not introspects:
            violations.append(
                f"{py.name}:{func.lineno} {func.name} ({path}): "
                "multi-column UPDATE without form/json introspection"
            )
    assert not violations, (
        "Edit routes that emit multi-column UPDATEs must introspect "
        "request.form() or request.json() so they only write columns "
        "the client actually submitted. Use submitted_subset() from "
        "app/routers/_form_helpers.py.\n  " + "\n  ".join(violations)
    )


# ── Bookend invariant: zero outstanding clobbers ─────────────────────


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Editor-clobber epic in progress — 11 routes pending conversion. "
        "PRs B/C/D will shrink _KNOWN_CLOBBER_ROUTES; PR-D removes this "
        "xfail marker once the allowlist is empty. Failing this xfail "
        "(i.e. the assertion passing) means the epic is done; succeeding "
        "(asserting fails) means there are still unconverted routes."
    ),
)
def test_zero_unconverted_clobber_routes_remain():
    """The bookend: when this test stops xfailing, the epic is done.

    Asserts the allowlist is empty AND every audited edit route now
    passes the two invariants above (which the per-route tripwires
    already check, so this is mostly redundant — but it's the explicit
    'we did the thing' marker)."""
    assert not _KNOWN_CLOBBER_ROUTES, (
        f"{len(_KNOWN_CLOBBER_ROUTES)} routes still in the clobber "
        f"allowlist — conversion PRs in progress: "
        f"{sorted(_KNOWN_CLOBBER_ROUTES)}"
    )


# ── Sanity: helper module is importable + has the expected surface ───


def test_form_helpers_module_exposes_expected_surface():
    """If anyone deletes _form_helpers.py or renames the public helpers,
    this fails immediately rather than at the first conversion PR."""
    from routers import _form_helpers as fh
    expected = {
        'submitted_subset', 'str_or_none', 'stripped_str',
        'int_or_none', 'fk_id_or_none', 'int_default_zero',
        'float_default_zero', 'bool_int', 'csv_to_json_array',
        'csv_to_int_set_str', 'parsed_json_str', 'parsed_tag_csv',
    }
    missing = expected - set(dir(fh))
    assert not missing, f"_form_helpers missing helpers: {missing}"


def test_audit_allowlist_entries_exist():
    """Lint test: every (file, method, path) in the allowlist must
    actually correspond to a real @router.post in the codebase. Catches
    typos and stale entries after a route gets renamed."""
    discovered: set[tuple[str, str, str]] = {
        (py.name, 'POST', path)
        for py, _func, path in _iter_post_routes()
        if _is_edit_route(path, _func.name)
    }
    stale = _KNOWN_CLOBBER_ROUTES - discovered
    assert not stale, (
        f"_KNOWN_CLOBBER_ROUTES contains entries that no longer exist "
        f"as @router.post handlers: {sorted(stale)}"
    )
