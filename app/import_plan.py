"""Import planning: build _ImportPlan from queue/series/files data."""

import logging
import os

from events import log_event
from parsing import extract_chapter_num
from files import build_filename, quality_from_filename, quality_rank, safe_join_under
from rescan import _series_library_dir

log = logging.getLogger(__name__)


class _FilePlan:
    """Frozen per-file decision data computed in Phase 1."""

    def __init__(
        self,
        file_id: int,
        src_path: str,
        filename: str,
        dst_path: str,
        file_type: str,
        proposed_vol: float | None,
        proposed_chap: float | None,
        chap_range_end: float | None,
        vol_range_start: float | None,
        vol_range_end: float | None,
        pack_type: str | None,
        is_special: int,
        has_volume_range: bool,
        is_legacy_chapter_stub: bool,
        is_legacy_chapter_recheck: bool,
        plan_status: str,
        plan_failure_reason: str,
    ):
        self.file_id = file_id
        self.src_path = src_path
        self.filename = filename
        self.dst_path = dst_path
        self.file_type = file_type
        self.proposed_vol = proposed_vol
        self.proposed_chap = proposed_chap
        self.chap_range_end = chap_range_end
        self.vol_range_start = vol_range_start
        self.vol_range_end = vol_range_end
        self.pack_type = pack_type
        self.is_special = is_special
        self.has_volume_range = has_volume_range
        self.is_legacy_chapter_stub = is_legacy_chapter_stub
        self.is_legacy_chapter_recheck = is_legacy_chapter_recheck
        self.plan_status = plan_status
        self.plan_failure_reason = plan_failure_reason


class _ImportPlan:
    """Phase 1 output: queue/series snapshot plus per-file plans."""

    def __init__(
        self,
        queue: dict,
        series: dict | None,
        series_tags: list[str],
        dst_dir: str,
        import_mode: str,
        now_ts,
        files: list[_FilePlan],
        series_id: int,
    ):
        self.queue = queue
        self.series = series
        self.series_tags = series_tags
        self.dst_dir = dst_dir
        self.import_mode = import_mode
        self.now_ts = now_ts
        self.files = files
        self.series_id = series_id


def _plan_import(
    db,
    queue_id: int,
    volume_overrides: dict,
    chapter_overrides: dict,
    skip_ids: set,
    import_mode: str,
):
    """Phase 1: read queue/series/files and build _ImportPlan."""
    queue_row = db.execute(
        "SELECT * FROM import_queue WHERE id=?", (queue_id,)
    ).fetchone()
    if not queue_row or queue_row["status"] not in ("pending", "partial", "importing"):
        return None
    queue = dict(queue_row)

    files = db.execute(
        "SELECT * FROM import_queue_files WHERE queue_id=? AND status IN ('pending', 'needs_review')",
        (queue_id,),
    ).fetchall()

    if not files:
        if queue["status"] == "importing":
            db.execute(
                "UPDATE import_queue SET status='pending' WHERE id=?", (queue_id,)
            )
        return None

    s_row = db.execute(
        "SELECT * FROM series WHERE id=?", (queue["series_id"],)
    ).fetchone()
    s = dict(s_row) if s_row else None
    series_tags = [
        r["tag"]
        for r in db.execute(
            "SELECT tag FROM series_tags WHERE series_id=?", (queue["series_id"],)
        ).fetchall()
    ]
    dst_dir = _series_library_dir(db, queue["series_id"]) if s else None
    if not dst_dir:
        log_event(
            "error",
            "Import: cannot resolve destination folder",
            queue["series_id"],
            db=db,
        )
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
        db.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL, import_path=NULL"
            " WHERE download_id=? AND status='grabbed'",
            (queue["download_id"],),
        )
        return None

    try:
        os.makedirs(dst_dir, exist_ok=True)
    except Exception as e:
        log_event(
            "error",
            f"Import: cannot create {dst_dir}: {e}",
            queue["series_id"],
            db=db,
        )
        db.execute("UPDATE import_queue SET status='failed' WHERE id=?", (queue_id,))
        db.execute(
            "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
            " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
            " client=NULL, release_group=NULL, import_path=NULL"
            " WHERE download_id=? AND status='grabbed'",
            (queue["download_id"],),
        )
        return None

    now_ts = None
    plans = []

    for f in files:
        if f["id"] in skip_ids:
            db.execute(
                "UPDATE import_queue_files SET status='skipped' WHERE id=?", (f["id"],)
            )
            plans.append(
                _FilePlan(
                    file_id=f["id"],
                    src_path=f["src_path"],
                    filename=f["filename"],
                    dst_path="",
                    file_type="",
                    proposed_vol=None,
                    proposed_chap=None,
                    chap_range_end=None,
                    vol_range_start=None,
                    vol_range_end=None,
                    pack_type=None,
                    is_special=0,
                    has_volume_range=False,
                    is_legacy_chapter_stub=False,
                    is_legacy_chapter_recheck=False,
                    plan_status="skip",
                    plan_failure_reason="",
                )
            )
            continue

        new_vol = volume_overrides.get(f["id"])
        new_chap = chapter_overrides.get(f["id"])
        if new_vol is not None:
            db.execute(
                "UPDATE import_queue_files SET proposed_volume=? WHERE id=?",
                (new_vol, f["id"]),
            )
        if new_chap is not None:
            db.execute(
                "UPDATE import_queue_files SET proposed_chapter=?, file_type='chapter' WHERE id=?",
                (new_chap, f["id"]),
            )

        _keys = f.keys()
        proposed_vol = (
            new_vol
            if new_vol is not None
            else (f["proposed_volume"] if "proposed_volume" in _keys else None)
        )
        proposed_chap = (
            new_chap
            if new_chap is not None
            else (f["proposed_chapter"] if "proposed_chapter" in _keys else None)
        )
        file_type = (
            "chapter"
            if new_chap is not None
            else (f["file_type"] if "file_type" in _keys else "volume")
        )

        _keys = f.keys()
        row_vol_rs = (
            f["proposed_volume_range_start"]
            if "proposed_volume_range_start" in _keys
            else None
        )
        row_vol_re = (
            f["proposed_volume_range_end"]
            if "proposed_volume_range_end" in _keys
            else None
        )
        row_chap_re = (
            f["proposed_chapter_range_end"]
            if "proposed_chapter_range_end" in _keys
            else None
        )
        row_pack_type = (
            f["proposed_pack_type"] if "proposed_pack_type" in _keys else None
        )
        row_is_special = (
            int(f["proposed_is_special"] or 0)
            if "proposed_is_special" in _keys and f["proposed_is_special"]
            else 0
        )

        is_legacy_chapter_recheck = False
        if (
            file_type == "volume"
            and proposed_vol is None
            and proposed_chap is None
            and f["id"] not in volume_overrides
        ):
            recheck_chap = extract_chapter_num(os.path.basename(f["src_path"]))
            if recheck_chap is not None:
                proposed_chap = recheck_chap
                file_type = "chapter"
                is_legacy_chapter_recheck = True
                db.execute(
                    "UPDATE import_queue_files SET proposed_chapter=?, file_type='chapter' WHERE id=?",
                    (recheck_chap, f["id"]),
                )

        has_vol_range = row_vol_rs is not None and row_vol_re is not None

        plan_status = "ready"
        plan_failure_reason = ""
        is_legacy_chapter_stub = False

        if (
            file_type == "volume"
            and proposed_vol is None
            and not has_vol_range
            and f["id"] not in volume_overrides
        ):
            stub = None
            if queue["download_id"]:
                stub = db.execute(
                    "SELECT id FROM volumes WHERE series_id=? AND download_id=?"
                    " AND status='grabbed' AND pack_type='chapter'",
                    (queue["series_id"], queue["download_id"]),
                ).fetchone()
            if stub:
                is_legacy_chapter_stub = True
            else:
                db.execute(
                    "UPDATE import_queue_files SET status='needs_review' WHERE id=?",
                    (f["id"],),
                )
                plan_status = "needs_review"

        filename = f["filename"]
        if (
            file_type == "chapter"
            and proposed_chap is not None
            and ("{Volume" in filename or "{Chapter" in filename)
        ):
            filename = build_filename(
                s["title"] if s else "",
                proposed_vol,
                os.path.basename(f["src_path"] or filename),
                chapter_num=proposed_chap,
            )
            db.execute(
                "UPDATE import_queue_files SET filename=? WHERE id=?",
                (filename, f["id"]),
            )

        if (
            plan_status == "ready"
            and file_type == "volume"
            and proposed_vol is not None
        ):
            existing = db.execute(
                "SELECT status, quality FROM volumes"
                " WHERE series_id=? AND volume_num=?",
                (queue["series_id"], proposed_vol),
            ).fetchone()
            new_quality = quality_from_filename(f["src_path"] or filename)
            if (
                existing
                and existing["status"] == "downloaded"
                and existing["quality"]
                and new_quality
                and quality_rank(existing["quality"]) >= quality_rank(new_quality)
            ):
                db.execute(
                    "UPDATE import_queue_files SET status='skipped' WHERE id=?",
                    (f["id"],),
                )
                plan_status = "skip"

        dst_path = ""
        if plan_status == "ready":
            try:
                dst_path = safe_join_under(dst_dir, filename)
            except ValueError as _e:
                plan_status = "pre_failed"
                plan_failure_reason = f"unsafe destination ({filename}): {_e}"
            if plan_status == "ready" and not os.path.isfile(f["src_path"]):
                plan_status = "pre_failed"
                plan_failure_reason = f"source file missing: {f['src_path']}"

        plans.append(
            _FilePlan(
                file_id=f["id"],
                src_path=f["src_path"],
                filename=filename,
                dst_path=dst_path,
                file_type=file_type,
                proposed_vol=proposed_vol,
                proposed_chap=proposed_chap,
                chap_range_end=row_chap_re,
                vol_range_start=row_vol_rs,
                vol_range_end=row_vol_re,
                pack_type=row_pack_type,
                is_special=row_is_special,
                has_volume_range=has_vol_range,
                is_legacy_chapter_stub=is_legacy_chapter_stub,
                is_legacy_chapter_recheck=is_legacy_chapter_recheck,
                plan_status=plan_status,
                plan_failure_reason=plan_failure_reason,
            )
        )

    now_ts = None
    if plans:
        now_ts = None

    return _ImportPlan(
        queue=queue,
        series=s,
        series_tags=series_tags,
        dst_dir=dst_dir,
        import_mode=import_mode,
        now_ts=now_ts,
        files=plans,
        series_id=queue["series_id"],
    )
