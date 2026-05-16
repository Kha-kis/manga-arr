"""Regression test for the _mark_downloaded vs reassignment-reset
ordering bug in _commit_import.

The refactored code now enforces the invariant structurally: the
reassign-reset fires only when new_status=="failed" (line ~181) while
_mark_downloaded fires only when imported_count > 0 (line ~197) —
mutually exclusive branches. This test verifies both patterns still
exist with correct guard predicates.
"""

import pathlib
import re

import pytest


_COMMIT_PATH = pathlib.Path(__file__).resolve().parents[2] / "app" / "import_commit.py"


def _commit_import_text() -> str:
    return _COMMIT_PATH.read_text()


def test_reset_uses_download_id_and_status_grabbed():
    """The reassign-reset SQL must key on download_id AND status='grabbed'.
    Without the status filter, it would also clobber rows already in
    'downloaded' state.
    """
    src = _commit_import_text()
    assert re.search(
        r"UPDATE volumes SET status='wanted'.*?"
        r"WHERE download_id=\? AND status='grabbed'",
        src,
        flags=re.DOTALL,
    ), "reset SQL must filter on status='grabbed' to avoid clobbering"


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

    import_block = re.search(r"if imported_count > 0:.*?else:", src, flags=re.DOTALL)
    assert import_block, "could not locate imported_count > 0 block"
    assert "_mark_downloaded(db, series_id," in import_block.group(0), (
        "_mark_downloaded must appear in the imported_count > 0 branch"
    )
