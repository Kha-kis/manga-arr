"""Regression test for the _mark_downloaded vs reassignment-reset
ordering bug in _commit_import.

The refactored code now enforces the invariant structurally: the
reassign-reset fires only when new_status=="failed" (line ~181) while
_mark_downloaded fires only when imported_count > 0 (line ~197) —
mutually exclusive branches. This test verifies both patterns still
exist with correct guard predicates.
"""

import ast
import pathlib
import re


_COMMIT_PATH = pathlib.Path(__file__).resolve().parents[2] / "app" / "import_commit.py"


def _commit_import_text() -> str:
    return _COMMIT_PATH.read_text()


def test_reset_uses_download_id_and_status_grabbed():
    """The reset must key on the full owner-qualified download identity.

    Without the series filter, a shared download ID could reset another
    series. Without the owner and protocol-aware ID filters, colliding client
    IDs could reset each other. Without the status filter, it could clobber
    downloaded rows.
    """
    src = _commit_import_text()
    string_literals = (
        node.value
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    reset_sql = next(
        (
            value
            for value in string_literals
            if re.search(r"UPDATE\s+volumes\s+SET\s+status\s*=\s*'wanted'", value)
        ),
        None,
    )
    assert reset_sql, "could not locate the reassign-reset SQL"
    normalized_sql = " ".join(reset_sql.split())
    required_filters = (
        "WHERE series_id=? AND download_client_id IS ?",
        "AND download_id IS NOT NULL",
        "(?='torrent' AND lower(download_id)=lower(?))",
        "(COALESCE(?,'')!='torrent' AND download_id=?)",
        "AND status='grabbed'",
    )
    assert all(fragment in normalized_sql for fragment in required_filters), (
        "reset SQL must filter by series, owner, protocol-aware download ID, "
        "and grabbed status"
    )


def test_reset_only_in_new_status_failed_branch():
    """The reset must only fire when new_status == 'failed' — it should
    NOT appear inside the imported_count > 0 branch (where _mark_downloaded
    lives)."""
    src = _commit_import_text()

    fail_block = re.search(
        r'new_status == "failed".*?if new_status == "imported"', src, flags=re.DOTALL
    )
    assert fail_block, "could not locate new_status == 'failed' block"
    assert "status='wanted'" in fail_block.group(0), (
        "reset SQL must appear in the new_status == 'failed' branch"
    )

    imported_branch = next(
        (
            node
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "imported_count"
            and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.Gt)
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == 0
        ),
        None,
    )
    assert imported_branch, "could not locate imported_count > 0 block"
    calls = (
        node
        for statement in imported_branch.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call)
    )
    assert any(
        isinstance(call.func, ast.Name)
        and call.func.id == "_mark_downloaded"
        and len(call.args) >= 2
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "db"
        and isinstance(call.args[1], ast.Name)
        and call.args[1].id == "series_id"
        for call in calls
    ), "_mark_downloaded must appear in the imported_count > 0 branch"
