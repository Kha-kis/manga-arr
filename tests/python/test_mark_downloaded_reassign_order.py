"""Regression test for the _mark_downloaded vs reassignment-reset
ordering bug in import_pipeline.py.

The bug (latent — flagged during the May-3 audit but not actively
firing in production data):

  if imported_count > 0:
      # OLD: this fired FIRST and flipped queue['volume_num'] from
      # grabbed → downloaded.
      _mark_downloaded(db, queue['series_id'], queue['volume_num'], ...)
      # The reassignment-reset below has WHERE status='grabbed',
      # which silently matched 0 rows because _mark_downloaded just
      # changed the row to 'downloaded'.
      if (queue['volume_num'] is not None
              and imported_vols
              and queue['volume_num'] not in imported_vols):
          db.execute(
              "UPDATE volumes SET status='wanted', ..."
              " WHERE series_id=? AND volume_num=? AND status='grabbed'", ...
          )

  Result for a review-form vol-reassignment (e.g. user grabbed v5 but
  the file actually lands as v6+v7 after they reassigned in the review
  UI): v5 ends up status='downloaded' with no actual file, while v6/v7
  get marked correctly via the per-file UPDATE loop.

The fix (PR following the audit): swap the order — run the reset
FIRST, then call _mark_downloaded. The reset's WHERE clause now
matches the still-'grabbed' row; _mark_downloaded becomes a no-op
(or a correct cascade) afterwards.

Architecture-style invariant: this test ensures the order doesn't get
re-flipped accidentally in a future refactor. A full integration test
of the reassignment path lives in test_import_atomicity.py's
exec_env-based suite — this one is the cheap tripwire.
"""
import pathlib
import re

import pytest


_PIPELINE_PATH = pathlib.Path(__file__).resolve().parents[2] / "app" / "import_pipeline.py"


def _import_pipeline_text() -> str:
    return _PIPELINE_PATH.read_text()


def test_reassignment_reset_appears_before_mark_downloaded_call():
    """The ordering invariant. The reassign-reset SQL block must appear
    earlier in source than the `_mark_downloaded(db, queue['series_id'],
    queue['volume_num']` call within the `if imported_count > 0:` block.

    If a future refactor moves _mark_downloaded above the reset, the
    reset's WHERE status='grabbed' clause silently misses (because
    _mark_downloaded already flipped the row to 'downloaded'), and the
    bug re-introduces.
    """
    src = _import_pipeline_text()

    # Locate the imported_count check block. Post-refactor (PR for
    # import lock contention) this block lives inside _commit_import
    # and is bounded by the function's `return (not any_error, ...)`
    # tuple — there's only one such return in the pipeline.
    m = re.search(r"if imported_count > 0:.*?return \(not any_error",
                  src, flags=re.DOTALL)
    assert m, "couldn't locate the post-import status block"
    block = m.group(0)

    # The reset SQL block fingerprint
    reset_idx = block.find("status='wanted', download_id=NULL")
    # The _mark_downloaded call fingerprint. Post-refactor _commit_import
    # uses a local `series_id` instead of `queue['series_id']`.
    mark_idx = block.find("_mark_downloaded(db, series_id,")

    assert reset_idx >= 0, "reassign-reset SQL not found in post-import block"
    assert mark_idx >= 0, "_mark_downloaded call not found in post-import block"
    assert reset_idx < mark_idx, (
        "BUG REGRESSION: _mark_downloaded must run AFTER the "
        "reassign-reset SQL — otherwise the reset's `status='grabbed'` "
        "clause silently matches 0 rows because _mark_downloaded just "
        "flipped the row to 'downloaded'. See "
        "test_mark_downloaded_reassign_order.py docstring for the full "
        "explanation. Swap the order back."
    )


def test_reassignment_reset_uses_grabbed_status_filter():
    """Companion check: the reset SQL must include `status='grabbed'`.
    Without it, the reset would also clobber rows already in
    'downloaded' state (e.g. if the user reset, re-grabbed, and the
    second grab succeeded).

    The fix preserves this filter — the reset only fires for rows
    still actively in the 'grabbed' state when the import completes.
    """
    src = _import_pipeline_text()
    # Look for the specific reset signature
    assert re.search(
        r"UPDATE volumes SET status='wanted'.*?WHERE series_id=\?"
        r" AND volume_num=\? AND status='grabbed'",
        src, flags=re.DOTALL
    ), "reset SQL must filter on status='grabbed' to avoid clobbering"


