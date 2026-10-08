"""Import planning: build _ImportPlan from queue/series/files data."""

import logging
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from acquisition_policy import acquisition_policy, policy_value

from download_identity import (
    DownloadIdentity,
    coerce_download_client_id,
    download_identities_match,
    normalize_download_protocol,
    normalize_download_id,
    resolve_download_protocol,
)
from events import log_event
from parsing import extract_chapter_num
from files import (
    QUALITY_RANK,
    build_filename,
    build_special_filename,
    derive_special_title,
    quality_from_filename,
    quality_rank,
    safe_join_under,
)
from import_kinds import normalize_import_kind
from import_lease import (
    has_import_sibling_that_may_use_download,
    refresh_import_queue_lease,
    release_import_queue_lease as _release_import_queue_lease,
    transition_import_queue_row as _transition_import_queue_row,
)
from rescan import _series_library_dir

log = logging.getLogger(__name__)


class _ImportPlanLeaseLost(RuntimeError):
    """Raised to roll back Phase 1 when its final ownership CAS fails."""


@dataclass(slots=True)
class _FilePlan:
    """Frozen per-file decision data computed in Phase 1."""

    file_id: int
    src_path: str
    filename: str
    dst_path: str
    import_kind: str
    file_type: str
    proposed_vol: float | None
    proposed_chap: float | None
    chap_range_end: float | None
    vol_range_start: float | None
    vol_range_end: float | None
    pack_type: str | None
    is_special: int
    special_title: str | None
    has_volume_range: bool
    is_legacy_chapter_stub: bool
    is_legacy_chapter_recheck: bool
    plan_status: str
    plan_failure_reason: str


@dataclass(slots=True)
class _ImportPlan:
    """Phase 1 output: queue/series snapshot plus per-file plans."""

    queue: dict[str, Any]
    series: dict[str, Any] | None
    series_tags: list[str]
    dst_dir: str
    import_mode: str
    now_ts: str | None
    files: list[_FilePlan]
    series_id: int


def _queue_identity(
    db: sqlite3.Connection, queue: Mapping[str, Any]
) -> DownloadIdentity:
    protocol = normalize_download_protocol(queue.get("download_protocol"))
    owner = coerce_download_client_id(queue.get("download_client_id"))
    if protocol is None:
        protocol = resolve_download_protocol(
            db,
            download_client_id=owner,
            series_id=int(queue["series_id"]),
            download_id=str(queue.get("download_id") or ""),
            source_url=str(queue.get("torrent_url") or ""),
            allow_client_configuration=False,
        )
    return DownloadIdentity(owner, protocol, str(queue.get("download_id") or ""))


def _manual_mapping_files(queue: Mapping[str, Any]) -> set[int]:
    values = queue.get("_manual_mapping_files", [])
    if not isinstance(values, list):
        return set()
    return {v for v in values if isinstance(v, int) and not isinstance(v, bool)}


def _claim_identity_matches(row: sqlite3.Row, identity: DownloadIdentity) -> bool:
    protocol = normalize_download_protocol(row["protocol"])
    if row["protocol"] and protocol is None:
        return False
    return download_identities_match(
        DownloadIdentity(
            coerce_download_client_id(row["download_client_id"]),
            protocol,
            str(row["download_id"] or ""),
        ),
        identity,
    )


def _automatic_grab_import(db: sqlite3.Connection, queue: Mapping[str, Any]) -> bool:
    persisted = policy_value(queue.get("respect_grab_claims"))
    if persisted is not None:
        return bool(persisted)
    if queue.get("_respect_grab_claims") is True:
        return True
    return acquisition_policy(
        db,
        series_id=int(queue["series_id"]),
        source_url=str(queue.get("torrent_url") or ""),
        identity=_queue_identity(db, queue),
    ) != 0


_PUBLICATION_ROW_FIELDS = (
    "id", "series_id", "status", "grabbed_at", "imported_at", "import_path",
    "quality", "size_bytes", "download_id", "download_client_id", "protocol",
    "torrent_name", "indexer", "client", "release_group",
)
_PUBLICATION_VOLUME_FIELDS = _PUBLICATION_ROW_FIELDS + (
    "volume_num", "chapter_num", "is_special", "pack_type", "vol_range_start",
    "vol_range_end", "edition_type", "language", "source_url",
)
_PUBLICATION_CHAPTER_FIELDS = _PUBLICATION_ROW_FIELDS + (
    "volume_id", "chapter_num", "chapter_range_end", "torrent_url",
)
_PUBLICATION_MAPPING_FIELDS = (
    "id", "queue_id", "src_path", "filename", "proposed_volume", "file_type",
    "proposed_chapter", "proposed_chapter_range_end", "proposed_volume_range_start",
    "proposed_volume_range_end", "proposed_pack_type", "proposed_is_special",
    "proposed_special_title", "proposed_import_kind",
)


def publication_admission(db: sqlite3.Connection, plan: _ImportPlan) -> dict[str, Any]:
    """SQL-only observation of every row the admitted batch can affect.

    Include owned acquisition ranges because Phase3 completion cascades across
    them, plus explicitly mapped targets/parents/children and path collisions.
    The absence of a matching row is an observation too. Display/monitoring
    fields deliberately do not revoke an otherwise intact admission.
    """
    from import_commit import _queue_metadata

    queue = plan.queue
    identity = _queue_identity(db, queue)
    current = db.execute("SELECT * FROM import_queue WHERE id=?", (queue["id"],)).fetchone()
    series = db.execute("SELECT * FROM series WHERE id=?", (plan.series_id,)).fetchone()
    volumes = db.execute("SELECT * FROM volumes WHERE series_id=? ORDER BY id", (plan.series_id,)).fetchall()
    chapters = db.execute("SELECT * FROM chapters WHERE series_id=? ORDER BY id", (plan.series_id,)).fetchall()
    source = str(queue.get("torrent_url") or "")
    ready = [fp for fp in plan.files if fp.plan_status == "ready"]
    owned_volumes = {
        r["id"] for r in volumes
        if r["download_client_id"] == identity.download_client_id
        and r["source_url"] == source and _claim_identity_matches(r, identity)
    }
    owned_chapters = {
        r["id"] for r in chapters
        if r["download_client_id"] == identity.download_client_id
        and r["torrent_url"] == source and _claim_identity_matches(r, identity)
    }
    entries: dict[str, Any] = {}
    for fp in ready:
        selected = {
            r["id"] for r in volumes
            if r["id"] in owned_volumes or r["import_path"] == fp.dst_path
            or (fp.proposed_vol is not None and r["volume_num"] == fp.proposed_vol)
            or (fp.has_volume_range and r["volume_num"] is not None
                and fp.vol_range_start is not None and fp.vol_range_end is not None
                and fp.vol_range_start <= r["volume_num"] <= fp.vol_range_end)
        }
        selected_chapters = {
            r["id"] for r in chapters
            if r["id"] in owned_chapters or r["volume_id"] in selected
            or r["import_path"] == fp.dst_path
            or (fp.proposed_chap is not None and r["chapter_num"] == fp.proposed_chap)
        }
        mapping = db.execute("SELECT * FROM import_queue_files WHERE id=?", (fp.file_id,)).fetchone()
        entries[str(fp.file_id)] = {
            "mapping": {k: mapping[k] for k in _PUBLICATION_MAPPING_FIELDS if mapping is not None and k in mapping.keys()},
            "effective": {k: getattr(fp, k) for k in (
                "import_kind", "file_type", "proposed_vol", "proposed_chap", "chap_range_end",
                "vol_range_start", "vol_range_end", "pack_type", "is_special", "special_title", "dst_path",
            )},
            "volumes": [{k: r[k] for k in _PUBLICATION_VOLUME_FIELDS if k in r.keys()} for r in volumes if r["id"] in selected],
            "chapters": [{k: r[k] for k in _PUBLICATION_CHAPTER_FIELDS if k in r.keys()} for r in chapters if r["id"] in selected_chapters],
            "other_path_claims": {
                table: [{k: row[k] for k in fields if k in row.keys()} for row in db.execute(
                    f"SELECT * FROM {table} WHERE series_id!=? AND import_path=? ORDER BY id",
                    (plan.series_id, fp.dst_path),
                )]
                for table, fields in (("volumes", _PUBLICATION_VOLUME_FIELDS), ("chapters", _PUBLICATION_CHAPTER_FIELDS))
            },
            "claims": _file_has_grab_claim(db, queue, fp),
        }
    return {
        "acquisition_metadata": _queue_metadata(db, queue, plan.series_id),
        "series": None if series is None else {k: series[k] for k in ("id", "root_folder_id", "deleted_at", "edition_type")},
        "queue": None if current is None else {k: current[k] for k in (
            "series_id", "torrent_url", "download_client_id", "download_id", "download_protocol", "respect_grab_claims",
        )},
        "identity": [identity.download_client_id, identity.protocol, normalize_download_id(identity.download_id, identity.protocol)],
        "policy": int(_automatic_grab_import(db, queue)),
        "manual_files": sorted(_manual_mapping_files(queue)),
        "files": entries,
    }


def _file_has_grab_claim(
    db: sqlite3.Connection, queue: Mapping[str, Any], fp: _FilePlan
) -> bool:
    if not _automatic_grab_import(db, queue) or fp.file_id in _manual_mapping_files(
        queue
    ):
        return True
    identity = _queue_identity(db, queue)
    if fp.proposed_chap is not None and fp.file_type == "chapter":
        table, url_column, coverage = "chapters", "torrent_url", "chapter_num=?"
        coverage_args = [fp.proposed_chap]
    elif fp.proposed_vol is not None:
        table, url_column, coverage = "volumes", "source_url", "volume_num=?"
        coverage_args = [fp.proposed_vol]
    elif fp.has_volume_range:
        # A claim for one covered volume permits a new range artifact, not
        # replacement of an existing artifact owned by an unrelated row.
        for artifact in db.execute(
            "SELECT * FROM volumes WHERE import_path=?", (fp.dst_path,)
        ):
            if (
                artifact["series_id"] != queue["series_id"]
                or artifact["status"] != "grabbed"
                or coerce_download_client_id(artifact["download_client_id"])
                != identity.download_client_id
                or artifact["source_url"] != str(queue.get("torrent_url") or "")
                or not _claim_identity_matches(artifact, identity)
            ):
                return False
        table, url_column, coverage = (
            "volumes",
            "source_url",
            "volume_num BETWEEN ? AND ?",
        )
        coverage_args = [fp.vol_range_start, fp.vol_range_end]
    else:
        # Specials and legacy unmapped records have no mainline target to replace.
        return True
    return any(
        _claim_identity_matches(row, identity)
        for row in db.execute(
            f"SELECT * FROM {table} WHERE series_id=? AND {coverage}"
            f" AND status='grabbed' AND download_client_id IS ? AND {url_column}=?",
            (
                queue["series_id"],
                *coverage_args,
                identity.download_client_id,
                str(queue.get("torrent_url") or ""),
            ),
        )
    )


def _plan_import(
    db: sqlite3.Connection,
    queue_id: int,
    lease_owner: str,
    volume_overrides: dict[int, float],
    chapter_overrides: dict[int, float],
    skip_ids: set[int],
    import_mode: str,
    *,
    lease_seconds: float,
    source_qualities: Mapping[int, str | None] | None = None,
) -> _ImportPlan | None:
    """Phase 1: read queue/series/files and build _ImportPlan."""
    queue_row = db.execute(
        "SELECT * FROM import_queue"
        " WHERE id=? AND status='importing' AND lease_owner=?"
        " AND lease_expires_at > datetime('now')",
        (queue_id, lease_owner),
    ).fetchone()
    if not queue_row:
        return None
    queue = dict(queue_row)
    # The publication journal persists this queue snapshot, including explicit
    # per-file review intent, so recovery cannot turn a manual mapping automatic.
    queue["_manual_mapping_files"] = sorted(
        set(volume_overrides) | set(chapter_overrides)
    )
    queue["_respect_grab_claims"] = _automatic_grab_import(db, queue)
    queue["respect_grab_claims"] = int(queue["_respect_grab_claims"])

    files = db.execute(
        "SELECT * FROM import_queue_files"
        " WHERE queue_id=? AND status IN ('pending','needs_review')",
        (queue_id,),
    ).fetchall()

    if not files:
        _release_import_queue_lease(db, queue_id, lease_owner)
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
        transitioned = _transition_import_queue_row(
            db,
            queue_id,
            lease_owner,
            "failed",
        )
        shared_download = has_import_sibling_that_may_use_download(
            db,
            queue_id=queue_id,
            download_id=queue["download_id"],
            download_client_id=queue.get("download_client_id"),
            series_id=queue["series_id"],
        )
        if transitioned and not shared_download:
            owner_id = coerce_download_client_id(queue.get("download_client_id"))
            protocol = resolve_download_protocol(
                db,
                download_client_id=owner_id,
                series_id=queue["series_id"],
                download_id=str(queue["download_id"] or ""),
                source_url=str(queue["torrent_url"] or ""),
            )
            db.execute(
                "UPDATE volumes SET status='wanted', grabbed_at=NULL, download_id=NULL,"
                " source_url=NULL, torrent_name=NULL, indexer=NULL, protocol=NULL,"
                " client=NULL, download_client_id=NULL, release_group=NULL,"
                " import_path=NULL"
                " WHERE series_id=? AND download_client_id IS ?"
                " AND download_id IS NOT NULL"
                " AND ("
                "   (?='torrent' AND lower(download_id)=lower(?))"
                "   OR (COALESCE(?,'')!='torrent' AND download_id=?)"
                " )"
                " AND status='grabbed'",
                (
                    queue["series_id"],
                    owner_id,
                    protocol,
                    queue["download_id"],
                    protocol,
                    queue["download_id"],
                ),
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
                    import_kind="skip",
                    file_type="",
                    proposed_vol=None,
                    proposed_chap=None,
                    chap_range_end=None,
                    vol_range_start=None,
                    vol_range_end=None,
                    pack_type=None,
                    is_special=0,
                    special_title=None,
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
        row_import_kind = (
            f["proposed_import_kind"] if "proposed_import_kind" in _keys else None
        )
        special_title = (
            f["proposed_special_title"] if "proposed_special_title" in _keys else None
        )
        import_kind = normalize_import_kind(
            row_import_kind,
            file_type=file_type,
            pack_type=row_pack_type,
            is_special=row_is_special,
            volume_range_end=row_vol_re,
            chapter_range_end=row_chap_re,
        )
        if new_chap is not None:
            import_kind = "chapter_range" if row_chap_re is not None else "chapter"
        elif new_vol is not None and import_kind != "special":
            import_kind = "volume_range" if row_vol_re is not None else "volume"

        if import_kind == "special":
            file_type = "special"
            row_is_special = 1
            proposed_vol = None
            proposed_chap = None
            row_vol_rs = None
            row_vol_re = None
            row_chap_re = None
            special_title = special_title or derive_special_title(
                s["title"] if s else "", f["src_path"] or f["filename"]
            )

        is_legacy_chapter_recheck = False
        if (
            import_kind == "volume"
            and proposed_vol is None
            and proposed_chap is None
            and f["id"] not in volume_overrides
        ):
            recheck_chap = extract_chapter_num(os.path.basename(f["src_path"]))
            if recheck_chap is not None:
                proposed_chap = recheck_chap
                file_type = "chapter"
                import_kind = "chapter"
                is_legacy_chapter_recheck = True
                db.execute(
                    "UPDATE import_queue_files SET proposed_chapter=?, file_type='chapter' WHERE id=?",
                    (recheck_chap, f["id"]),
                )

        has_vol_range = row_vol_rs is not None and row_vol_re is not None

        plan_status = "needs_review" if f["status"] == "needs_review" else "ready"
        plan_failure_reason = ""
        is_legacy_chapter_stub = False

        if (
            import_kind == "volume"
            and proposed_vol is None
            and not has_vol_range
            and f["id"] not in volume_overrides
        ):
            stub = None
            owner_id = coerce_download_client_id(queue.get("download_client_id"))
            if queue["download_id"] and owner_id is not None:
                protocol = resolve_download_protocol(
                    db,
                    download_client_id=owner_id,
                    series_id=queue["series_id"],
                    download_id=str(queue["download_id"]),
                    source_url=str(queue["torrent_url"] or ""),
                )
                stub = db.execute(
                    "SELECT id FROM volumes WHERE series_id=?"
                    " AND download_id IS NOT NULL AND download_client_id IS ?"
                    " AND ("
                    "   (?='torrent' AND lower(download_id)=lower(?))"
                    "   OR (COALESCE(?,'')!='torrent' AND download_id=?)"
                    " )"
                    " AND status='grabbed' AND pack_type='chapter'",
                    (
                        queue["series_id"],
                        owner_id,
                        protocol,
                        queue["download_id"],
                        protocol,
                        queue["download_id"],
                    ),
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
        if import_kind == "special":
            filename = build_special_filename(
                s["title"] if s else "", special_title or "Special", f["src_path"]
            )
            db.execute(
                "UPDATE import_queue_files SET filename=?, proposed_special_title=?,"
                " proposed_is_special=1, proposed_import_kind='special' WHERE id=?",
                (filename, special_title, f["id"]),
            )
        elif (
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
        elif (
            import_kind == "volume"
            and proposed_vol is not None
            and "{Volume" in filename
        ):
            filename = build_filename(
                s["title"] if s else "",
                proposed_vol,
                os.path.basename(f["src_path"] or filename),
            )
            db.execute(
                "UPDATE import_queue_files SET filename=? WHERE id=?",
                (filename, f["id"]),
            )

        if (
            plan_status == "ready"
            and import_kind == "volume"
            and proposed_vol is not None
        ):
            existing = db.execute(
                "SELECT status, quality FROM volumes"
                " WHERE series_id=? AND volume_num=?",
                (queue["series_id"], proposed_vol),
            ).fetchone()
            if source_qualities is not None:
                new_quality = source_qualities.get(f["id"])
            else:
                # SQL-only legacy callers have no magic-byte observation.
                extension = os.path.splitext(f["src_path"] or filename)[1].lower().lstrip(".")
                new_quality = extension if extension in QUALITY_RANK else None
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
        plans.append(
            _FilePlan(
                file_id=f["id"],
                src_path=f["src_path"],
                filename=filename,
                dst_path=dst_path,
                import_kind=import_kind,
                file_type=file_type,
                proposed_vol=proposed_vol,
                proposed_chap=proposed_chap,
                chap_range_end=row_chap_re,
                vol_range_start=row_vol_rs,
                vol_range_end=row_vol_re,
                pack_type=row_pack_type,
                is_special=row_is_special,
                special_title=special_title,
                has_volume_range=has_vol_range,
                is_legacy_chapter_stub=is_legacy_chapter_stub,
                is_legacy_chapter_recheck=is_legacy_chapter_recheck,
                plan_status=plan_status,
                plan_failure_reason=plan_failure_reason,
            )
        )
        if plans[-1].plan_status == "ready" and not _file_has_grab_claim(
            db, queue, plans[-1]
        ):
            plans[-1].plan_status = "skip"
            plans[-1].dst_path = ""
            db.execute(
                "UPDATE import_queue_files SET status='skipped' WHERE id=?", (f["id"],)
            )

    now_ts = None
    if plans:
        now_ts = None

    plan = _ImportPlan(
        queue=queue,
        series=s,
        series_tags=series_tags,
        dst_dir=dst_dir,
        import_mode=import_mode,
        now_ts=now_ts,
        files=plans,
        series_id=queue["series_id"],
    )
    # Phase 1 may update child decisions above. This renewal is deliberately
    # its final DB mutation so an expired/stale owner rolls the transaction
    # back instead of committing any of those child changes.
    db.execute(
        "UPDATE import_queue SET respect_grab_claims=?"
        " WHERE id=? AND lease_owner=? AND respect_grab_claims IS NULL",
        (queue["respect_grab_claims"], queue_id, lease_owner),
    )
    if not refresh_import_queue_lease(
        db,
        queue_id,
        lease_owner,
        lease_seconds=lease_seconds,
    ):
        raise _ImportPlanLeaseLost
    queue["_file_publication"] = {
        "version": 1, "decision": "pending", "admission": publication_admission(db, plan),
        "artifacts": {},
    }
    return plan
