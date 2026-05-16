"""The import pipeline: discover completed downloads, queue them, and stage/commit files into the library.

This module re-exports symbols from the split modules:
  - import_discovery.py: discovery logic (qBittorrent/SABnzbd polling)
  - import_queue.py: file classification and queue entry creation
  - import_staging.py: two-phase commit staging helpers
  - import_execute.py: execution orchestration and three-phase pipeline

For the full pipeline implementation, see the split modules.
"""

from __future__ import annotations

# Re-export from split modules
from import_discovery import (
    _CHECK_DOWNLOAD_STATUS_LOCK,
    check_download_status,
    _check_download_status_impl,
    _process_auto_import,
)
from import_queue import _queue_import
from import_staging import (
    _ImportStaging,
    _StageOutcome,
    _stage_files,
    _cleanup_pack_staging_dir,
    PACK_STAGING_ROOT,
)
from import_execute import (
    _FilePlan,
    _ImportPlan,
    claim_import_queue_row,
    _guarded_execute_import,
    _execute_import,
    _execute_import_impl,
    _mark_downloaded,
    initialize_import_semaphore,
    _IMPORT_SEM,
    _get_import_sem,
)

from comicinfo import _try_inject_comicinfo
from notifications import notify_discord, make_complete_embed, trigger_komga_scan
from events import broadcast_queue_event, add_history
from comicinfo import _try_inject_comicinfo
