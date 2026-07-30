"""Import queueing: scan completed downloads, classify files, build queue entries."""

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import zipfile
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from files import (
    MANGA_EXTENSIONS,
    build_filename,
    build_special_filename,
    derive_special_title,
    sanitize_filename,
)
from parsing import (
    _parse_vol_suffix,
    detect_pack_type,
    extract_chapter_num,
    extract_chapter_range,
    extract_volume_num,
    extract_volume_range,
    is_foreign_language,
    is_special_release,
)
from shared import get_cfg, get_db
from comicinfo import read_comic_info
from download_identity import (
    DownloadIdentity,
    DownloadProtocol,
    coerce_download_client_id,
    download_identities_match,
    normalize_download_protocol,
    resolve_download_protocol,
)
from events import add_history, log_event
from import_kinds import infer_import_kind
from import_pack_cleanup import (
    PACK_RESERVATION_SECONDS,
    begin_pack_queue_attachment,
    durably_attach_pack_queue_directory,
    pack_queue_creation_paths,
    recover_pack_cleanup_state,
    refresh_pack_queue_creation,
    release_pack_queue_creation,
    remove_pack_queue_private_artifacts,
    reserve_pack_queue_creation,
)
from rescan import _series_library_dir


_SPLIT_RAR_PART_RE = re.compile(r"^(?P<stem>.+)\.(?:rar|r\d{2})$", re.IGNORECASE)
_IMAGE_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))
_P = ParamSpec("_P")
_T = TypeVar("_T")


class _PackQueueReservationLost(RuntimeError):
    """Raised once queue generation no longer owns its reservation."""


class _PackQueueHeartbeat:
    """Throttled synchronous heartbeat used at every filesystem checkpoint."""

    def __init__(
        self,
        db,
        download_id: str,
        download_client_id: int | None,
        protocol: DownloadProtocol | None,
        owner_token: str,
    ) -> None:
        self._db = db
        self._download_id = download_id
        self._download_client_id = download_client_id
        self._protocol: DownloadProtocol | None = protocol
        self._owner_token = owner_token
        self._interval = max(0.01, min(30.0, PACK_RESERVATION_SECONDS / 3))
        self._next_refresh = time.monotonic() + self._interval
        self._lost = threading.Event()

    def checkpoint(self, *, force: bool = False) -> None:
        if self._lost.is_set():
            self._raise_lost()
        now = time.monotonic()
        if not force and now < self._next_refresh:
            return
        if refresh_pack_queue_creation(
            self._db,
            self._download_id,
            self._owner_token,
            download_client_id=self._download_client_id,
            protocol=self._protocol,
            lease_seconds=PACK_RESERVATION_SECONDS,
            commit=True,
        ):
            self._next_refresh = now + self._interval
            return
        self._raise_lost()

    def run(self, operation: Callable[[], _T]) -> _T:
        """Keep the lease live while one indivisible scan operation blocks."""
        self.checkpoint(force=True)
        stop = threading.Event()

        def _keep_alive() -> None:
            while not stop.wait(self._interval):
                try:
                    with get_db() as heartbeat_db:
                        owned = refresh_pack_queue_creation(
                            heartbeat_db,
                            self._download_id,
                            self._owner_token,
                            download_client_id=self._download_client_id,
                            protocol=self._protocol,
                            lease_seconds=PACK_RESERVATION_SECONDS,
                            commit=False,
                        )
                except sqlite3.Error:
                    continue
                if not owned:
                    self._lost.set()
                    return

        worker = threading.Thread(
            target=_keep_alive,
            name=f"pack-queue-heartbeat-{self._download_id}",
            daemon=True,
        )
        worker.start()
        try:
            result = operation()
        finally:
            stop.set()
            worker.join(timeout=max(1.0, self._interval * 2))
        self.checkpoint(force=True)
        return result

    def _raise_lost(self) -> None:
        remove_pack_queue_private_artifacts(
            self._download_id,
            self._owner_token,
            download_client_id=self._download_client_id,
            protocol=self._protocol,
        )
        release_pack_queue_creation(
            self._db,
            self._download_id,
            self._owner_token,
            download_client_id=self._download_client_id,
            protocol=self._protocol,
            commit=True,
            attaching=True,
        )
        raise _PackQueueReservationLost


def _return_on_pack_reservation_loss(
    func: Callable[_P, tuple[int | None, bool]],
) -> Callable[_P, tuple[int | None, bool]]:
    @wraps(func)
    def _wrapped(*args: _P.args, **kwargs: _P.kwargs) -> tuple[int | None, bool]:
        try:
            return func(*args, **kwargs)
        except _PackQueueReservationLost:
            return None, False

    return _wrapped


def _persisted_download_identity(
    db: sqlite3.Connection,
    *,
    series_id: int,
    download_id: str,
    torrent_url: str,
) -> tuple[int | None, DownloadProtocol | None]:
    """Return one exact grab-time identity, or legacy NULL when unprovable.

    The source URL is the stable acquisition identity shared by ``seen`` and
    owned rows. Legacy rows and conflicting evidence intentionally remain
    unbound so later cleanup cannot guess from current routing configuration.
    """
    if not torrent_url:
        return None, None
    rows = db.execute(
        """
        SELECT download_client_id, protocol
        FROM seen
        WHERE series_id=? AND torrent_url=?
          AND (
              download_id=?
              OR lower(download_id)=lower(?)
          )
        UNION ALL
        SELECT download_client_id, protocol
        FROM volumes
        WHERE series_id=? AND source_url=?
          AND (
              download_id=?
              OR lower(download_id)=lower(?)
          )
        UNION ALL
        SELECT download_client_id, protocol
        FROM chapters
        WHERE series_id=? AND torrent_url=?
          AND (
              download_id=?
              OR lower(download_id)=lower(?)
          )
        """,
        (
            series_id,
            torrent_url,
            download_id,
            download_id,
            series_id,
            torrent_url,
            download_id,
            download_id,
            series_id,
            torrent_url,
            download_id,
            download_id,
        ),
    ).fetchall()
    owners = {
        coerce_download_client_id(row["download_client_id"])
        for row in rows
    }
    protocols: set[DownloadProtocol] = {
        normalized
        for row in rows
        if (normalized := normalize_download_protocol(row["protocol"])) is not None
    }
    owner = next(iter(owners)) if len(owners) == 1 else None
    protocol = next(iter(protocols)) if len(protocols) == 1 else None
    if owner is None:
        return None, protocol
    return owner, protocol or resolve_download_protocol(
        db,
        download_client_id=owner,
        series_id=series_id,
        download_id=download_id,
        source_url=torrent_url,
        allow_client_configuration=False,
    )


def _matching_queue_rows(
    db: sqlite3.Connection,
    *,
    series_id: int,
    identity: DownloadIdentity,
) -> list[dict[str, Any]]:
    """Snapshot queue rows that match one ownership-aware download identity."""
    rows = db.execute(
        """
        SELECT id, status, download_id, download_client_id, download_protocol,
               torrent_url, series_id
        FROM import_queue
        WHERE series_id=? AND download_id IS NOT NULL
          AND (
              download_id=?
              OR lower(download_id)=lower(?)
          )
        ORDER BY id
        """,
        (series_id, identity.download_id, identity.download_id),
    ).fetchall()
    matching: list[dict[str, Any]] = []
    for row in rows:
        candidate = dict(row)
        candidate_owner = coerce_download_client_id(
            candidate["download_client_id"]
        )
        candidate_protocol = normalize_download_protocol(
            candidate["download_protocol"]
        )
        if candidate_protocol is None:
            candidate_protocol = resolve_download_protocol(
                db,
                download_client_id=candidate_owner,
                series_id=series_id,
                download_id=str(candidate["download_id"] or ""),
                source_url=str(candidate["torrent_url"] or ""),
            )
        if download_identities_match(
            identity,
            DownloadIdentity(
                candidate_owner,
                candidate_protocol,
                str(candidate["download_id"] or ""),
            ),
        ):
            matching.append(candidate)
    return matching


def _has_terminal_download_receipt(
    db: sqlite3.Connection,
    *,
    series_id: int,
    torrent_url: str,
    identity: DownloadIdentity,
) -> bool:
    """Return whether this exact acquisition identity was already handled."""
    domain_rows = db.execute(
        """
        SELECT download_id, download_client_id, protocol, source_url AS item_url
        FROM volumes
        WHERE series_id=? AND status='downloaded' AND download_id IS NOT NULL
          AND (
              download_id=?
              OR lower(download_id)=lower(?)
          )
        UNION ALL
        SELECT download_id, download_client_id, protocol, torrent_url AS item_url
        FROM chapters
        WHERE series_id=? AND status='downloaded' AND download_id IS NOT NULL
          AND (
              download_id=?
              OR lower(download_id)=lower(?)
          )
        """,
        (
            series_id,
            identity.download_id,
            identity.download_id,
            series_id,
            identity.download_id,
            identity.download_id,
        ),
    ).fetchall()
    for row in domain_rows:
        candidate_protocol = normalize_download_protocol(row["protocol"])
        candidate_owner = coerce_download_client_id(row["download_client_id"])
        if download_identities_match(
            identity,
            DownloadIdentity(
                candidate_owner,
                candidate_protocol,
                str(row["download_id"] or ""),
            ),
        ):
            return True

    history = db.execute(
        """
        SELECT download_id, download_client_id, protocol
        FROM history
        WHERE series_id=?
          AND (torrent_url=? OR torrent_url IS NULL)
          AND event_type IN ('imported','import_skipped')
          AND download_id IS NOT NULL
          AND (
              download_id=?
              OR lower(download_id)=lower(?)
          )
        """,
        (
            series_id,
            torrent_url,
            identity.download_id,
            identity.download_id,
        ),
    ).fetchall()
    return any(
        download_identities_match(
            identity,
            DownloadIdentity(
                coerce_download_client_id(row["download_client_id"]),
                normalize_download_protocol(row["protocol"]),
                str(row["download_id"] or ""),
            ),
        )
        for row in history
    )


@_return_on_pack_reservation_loss
def _queue_import(
    db,
    series_id: int,
    download_id: str,
    torrent_name: str,
    torrent_url: str,
    volume_num: float | None,
    content_path: str,
    *,
    download_client_id: int | None = None,
    protocol: str | None = None,
) -> tuple[int | None, bool]:
    """
    Scan completed download files at content_path and create an import_queue entry.
    Returns (queue_id, needs_review).
    needs_review=False means all files mapped cleanly → can auto-import.
    needs_review=True means at least one file is ambiguous → requires user review.
    """
    if not content_path:
        log_event(
            "error",
            f"Import queue: no content_path for {torrent_name}",
            series_id,
            db=db,
        )
        return None, False

    s = db.execute(
        "SELECT title, root_folder_id, chapter_vol_map, total_volumes FROM series WHERE id=?",
        (series_id,),
    ).fetchone()
    if not s:
        return None, False
    _total_vols = s["total_volumes"] if "total_volumes" in s.keys() else None
    scan_events: list[tuple[str, str, bool]] = []

    def _defer_scan_event(
        event_type: str,
        message: str,
        *,
        dedup: bool = False,
    ) -> None:
        scan_events.append((event_type, message, dedup))

    def _replay_scan_events() -> None:
        for event_type, message, dedup in scan_events:
            log_event(event_type, message, series_id, db=db, dedup=dedup)

    _rel_vol_range = extract_volume_range(torrent_name or "")
    _rel_chap_range = extract_chapter_range(torrent_name or "")
    _rel_is_special = is_special_release(torrent_name or "")
    _rel_pack_type = detect_pack_type(torrent_name or "", _rel_vol_range, _total_vols)

    normalized_protocol = normalize_download_protocol(protocol)
    if protocol is None:
        owner_id, normalized_protocol = _persisted_download_identity(
            db,
            series_id=series_id,
            download_id=download_id,
            torrent_url=torrent_url,
        )
    else:
        owner_id = coerce_download_client_id(download_client_id)
        normalized_protocol = normalized_protocol or resolve_download_protocol(
            db,
            download_client_id=owner_id,
            series_id=series_id,
            download_id=download_id,
            source_url=torrent_url,
            allow_client_configuration=False,
        )
    identity = DownloadIdentity(owner_id, normalized_protocol, download_id)

    # Existing queue state is authoritative. In particular, a mixed import can
    # have a durable imported receipt while a sibling file still needs review.
    # Never let terminal evidence hide or rewrite that unresolved work.
    existing_rows = _matching_queue_rows(
        db,
        series_id=series_id,
        identity=identity,
    )
    if existing_rows:
        existing = existing_rows[0]
        if existing["status"] in ("pending", "partial"):
            has_review = db.execute(
                "SELECT 1 FROM import_queue_files WHERE queue_id=? AND status='needs_review'",
                (existing["id"],),
            ).fetchone()
            if existing["status"] == "pending" or has_review:
                return existing["id"], bool(has_review)
        return None, False

    # A successfully handled import must remain terminal even when it updated
    # an existing chapter/volume, imported a special without creating a new
    # volume, or intentionally skipped every file because the library already
    # satisfied the import. The history row is the durable receipt after
    # completed queue rows are removed. Ignore empty IDs: they are not unique
    # download identities.
    if download_id and _has_terminal_download_receipt(
        db,
        series_id=series_id,
        torrent_url=torrent_url,
        identity=identity,
    ):
        return None, False

    recover_pack_cleanup_state(max_rows=20)
    pack_reservation_owner = reserve_pack_queue_creation(
        db,
        download_id,
        download_client_id=owner_id,
        protocol=normalized_protocol,
    )
    if pack_reservation_owner is None:
        return None, False
    heartbeat = _PackQueueHeartbeat(
        db,
        download_id,
        owner_id,
        normalized_protocol,
        pack_reservation_owner,
    )
    canonical_pack_dir, private_pack_dir = pack_queue_creation_paths(
        download_id,
        pack_reservation_owner,
        download_client_id=owner_id,
        protocol=normalized_protocol,
    )
    generated_pack_artifacts = False

    cvm: dict[str, Any] = (
        json.loads(s["chapter_vol_map"]) if s["chapter_vol_map"] else {}
    )

    if os.path.isdir(content_path):
        src_dir = content_path
        scan_paths = None

        image_leafs = sorted(
            _find_image_only_chapter_dirs(
                content_path,
                heartbeat.checkpoint,
            )
        )
        if image_leafs:
            heartbeat.checkpoint(force=True)
            pack_dir = private_pack_dir
            packed_paths: list[str] = []
            used_names: set[str] = set()
            for leaf in image_leafs:
                heartbeat.checkpoint()
                leaf_basename = os.path.basename(leaf.rstrip("/")) or "chapter"
                base_name = sanitize_filename(leaf_basename)
                cbz_name = base_name + ".cbz"
                n = 2
                while cbz_name in used_names:
                    cbz_name = f"{base_name} ({n}).cbz"
                    n += 1
                used_names.add(cbz_name)
                cbz_path = os.path.join(pack_dir, cbz_name)
                size = _pack_image_dir_to_cbz(
                    leaf,
                    cbz_path,
                    heartbeat.checkpoint,
                )
                if size:
                    packed_paths.append(cbz_path)
                else:
                    _defer_scan_event(
                        "error",
                        f"Auto-pack failed for {leaf}: "
                        f"check disk space + /config writable",
                        dedup=True,
                    )
            if packed_paths:
                _defer_scan_event(
                    "import",
                    f"Auto-packed {len(packed_paths)} image-only chapter "
                    f"director{'ies' if len(packed_paths) != 1 else 'y'} "
                    f"into CBZs: {torrent_name}",
                )
                scan_paths = packed_paths
                generated_pack_artifacts = True
        if scan_paths is None:
            heartbeat.checkpoint(force=True)
            split_payloads = _extract_zip_wrapped_split_rars(
                content_path,
                private_pack_dir,
                _defer_scan_event,
                heartbeat,
            )
            if split_payloads is not None:
                scan_paths = split_payloads
                generated_pack_artifacts = bool(split_payloads)
    elif os.path.isfile(content_path):
        src_dir = os.path.dirname(content_path)
        scan_paths = [content_path]
    else:
        remove_pack_queue_private_artifacts(
            download_id,
            pack_reservation_owner,
            download_client_id=owner_id,
            protocol=normalized_protocol,
        )
        release_pack_queue_creation(
            db,
            download_id,
            pack_reservation_owner,
            download_client_id=owner_id,
            protocol=normalized_protocol,
            commit=True,
        )
        log_event(
            "error",
            f"Import queue: content_path not found: {content_path}",
            series_id,
            db=db,
            dedup=True,
        )
        return None, False

    dst_dir = _series_library_dir(db, series_id)
    if not dst_dir:
        remove_pack_queue_private_artifacts(
            download_id,
            pack_reservation_owner,
            download_client_id=owner_id,
            protocol=normalized_protocol,
        )
        release_pack_queue_creation(
            db,
            download_id,
            pack_reservation_owner,
            download_client_id=owner_id,
            protocol=normalized_protocol,
            commit=True,
        )
        log_event(
            "error",
            f"Import queue: cannot resolve destination folder for {torrent_name}",
            series_id,
            db=db,
            dedup=True,
        )
        return None, False

    _chap_stub = db.execute(
        "SELECT id FROM volumes WHERE series_id=? AND download_id IS NOT NULL"
        " AND download_client_id IS ?"
        " AND ("
        "   (?='torrent' AND lower(download_id)=lower(?))"
        "   OR (COALESCE(?,'')!='torrent' AND download_id=?)"
        " )"
        " AND status='grabbed' AND pack_type='chapter'",
        (
            series_id,
            owner_id,
            normalized_protocol,
            download_id,
            normalized_protocol,
            download_id,
        ),
    ).fetchone()
    _is_chapter_grab = _chap_stub is not None

    if scan_paths is None:
        scan_paths = []
        for root, dirs, files in os.walk(src_dir):
            heartbeat.checkpoint()
            dirs.sort()
            for fname in sorted(files):
                heartbeat.checkpoint()
                scan_paths.append(os.path.join(root, fname))

    mapped = unmapped = special = 0
    file_rows = []
    for src_path in scan_paths:
        heartbeat.checkpoint()
        fname = os.path.basename(src_path)
        if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
            continue

        if is_foreign_language(fname):
            _defer_scan_event(
                "import",
                f"Skipped foreign-language file: {fname}",
            )
            continue

        proposed_vol = extract_volume_num(fname)
        proposed_chap = extract_chapter_num(fname)
        file_vol_range = extract_volume_range(fname)
        file_chap_range = extract_chapter_range(fname)
        proposed_vol_rs: float | None = None
        proposed_vol_re: float | None = None
        proposed_chap_re: float | None = None
        if file_vol_range is not None:
            proposed_vol_rs, proposed_vol_re = file_vol_range
            proposed_vol = None
        if file_chap_range is not None:
            proposed_chap, proposed_chap_re = file_chap_range
        proposed_is_special = int(_rel_is_special or is_special_release(fname))
        special += proposed_is_special

        ext_lower = os.path.splitext(fname)[1].lower()
        if ext_lower in (".cbz", ".zip"):
            ci = heartbeat.run(lambda: read_comic_info(src_path))
            if ci.get("volume") is not None:
                ci_vol = ci["volume"]
                if ci_vol != proposed_vol:
                    _defer_scan_event(
                        "import",
                        f"ComicInfo.xml: vol {proposed_vol} → {ci_vol} for {fname}",
                    )
                    proposed_vol = ci_vol
                    proposed_chap = None
                    proposed_vol_rs = None
                    proposed_vol_re = None
                    proposed_chap_re = None
            elif ci.get("number") is not None and proposed_chap is None:
                proposed_chap = ci["number"]
        elif ext_lower == ".cbr":
            try:
                import rarfile

                with rarfile.RarFile(src_path) as rf:
                    ci_name = heartbeat.run(
                        lambda: next(
                            (
                                name
                                for name in rf.namelist()
                                if name.lower().endswith("comicinfo.xml")
                            ),
                            None,
                        )
                    )
                    if ci_name:
                        from defusedxml.ElementTree import (
                            fromstring as _safe_xml_fromstring,
                        )

                        cbr_root = _safe_xml_fromstring(
                            heartbeat.run(lambda: rf.read(ci_name))
                        )

                        def _cbr_text(tag: str):
                            el = cbr_root.find(tag)
                            return (
                                el.text.strip() if el is not None and el.text else None
                            )

                        _raw_vol = _cbr_text("Volume")
                        _raw_num = _cbr_text("Number")
                        if _raw_vol:
                            ci_vol = _parse_vol_suffix(_raw_vol)
                            if ci_vol is not None:
                                if ci_vol != proposed_vol:
                                    _defer_scan_event(
                                        "import",
                                        f"ComicInfo.xml (CBR): vol {proposed_vol} → {ci_vol} for {fname}",
                                    )
                                proposed_vol = ci_vol
                                proposed_chap = None
                                proposed_vol_rs = None
                                proposed_vol_re = None
                                proposed_chap_re = None
                        elif _raw_num and proposed_chap is None:
                            ci_num = _parse_vol_suffix(_raw_num)
                            if ci_num is not None:
                                proposed_chap = ci_num
            except ImportError:
                pass
            except _PackQueueReservationLost:
                raise
            except Exception:
                pass

        if (
            volume_num is not None
            and _rel_pack_type == "volume"
            and not _is_chapter_grab
            and len(scan_paths) == 1
        ):
            proposed_vol = volume_num
            proposed_chap = None
            file_vol_range = None
            file_chap_range = None
            proposed_vol_rs = None
            proposed_vol_re = None
            proposed_chap_re = None

        has_chap_signal = proposed_chap is not None or proposed_chap_re is not None
        has_vol_signal = proposed_vol is not None or proposed_vol_re is not None

        if has_chap_signal and not has_vol_signal:
            file_type = "chapter"
            _key_src = proposed_chap if proposed_chap is not None else proposed_chap_re
            if _key_src is not None:
                chap_key = (
                    str(int(_key_src)) if _key_src == int(_key_src) else str(_key_src)
                )
                if chap_key in cvm:
                    proposed_vol = float(cvm[chap_key])
        else:
            file_type = "volume"
            proposed_chap = None
            proposed_chap_re = None

        if (
            proposed_vol is None
            and proposed_vol_rs is None
            and volume_num is not None
            and file_type == "volume"
        ):
            proposed_vol = volume_num

        dst_fname = build_filename(
            s["title"],
            proposed_vol,
            fname,
            chapter_num=proposed_chap if file_type == "chapter" else None,
        )
        dst_path = os.path.join(dst_dir, dst_fname)

        if _rel_pack_type == "complete":
            proposed_pack_type: str | None = "complete"
        elif proposed_chap_re is not None:
            proposed_pack_type = "chapter_range"
        elif proposed_vol_re is not None:
            proposed_pack_type = "volume_range"
        elif _rel_pack_type in ("chapter", "volume"):
            proposed_pack_type = _rel_pack_type
        else:
            proposed_pack_type = None

        proposed_import_kind = infer_import_kind(
            file_type=file_type,
            pack_type=proposed_pack_type,
            is_special=proposed_is_special,
            volume_range_end=proposed_vol_re,
            chapter_range_end=proposed_chap_re,
        )
        proposed_special_title = None
        if proposed_import_kind == "special":
            proposed_special_title = derive_special_title(s["title"], fname)
            dst_fname = build_special_filename(
                s["title"], proposed_special_title, fname
            )
            dst_path = os.path.join(dst_dir, dst_fname)

        is_unmapped = (
            proposed_vol is None
            and proposed_chap is None
            and proposed_vol_rs is None
            and proposed_chap_re is None
            and not _is_chapter_grab
        )
        if is_unmapped:
            unmapped += 1
        else:
            mapped += 1
        file_status = (
            "needs_review" if is_unmapped or proposed_is_special else "pending"
        )
        file_rows.append(
            (
                dst_fname,
                src_path,
                dst_path,
                proposed_vol,
                proposed_chap,
                proposed_vol_rs,
                proposed_vol_re,
                proposed_chap_re,
                proposed_pack_type,
                proposed_is_special,
                proposed_import_kind,
                proposed_special_title,
                file_type,
                file_status,
            )
        )

    if mapped == 0 and unmapped == 0:
        remove_pack_queue_private_artifacts(
            download_id,
            pack_reservation_owner,
            download_client_id=owner_id,
            protocol=normalized_protocol,
        )
        release_pack_queue_creation(
            db,
            download_id,
            pack_reservation_owner,
            download_client_id=owner_id,
            protocol=normalized_protocol,
            commit=True,
        )
        _defer_scan_event(
            "import",
            f"No manga files found in {src_dir} — skipping: {torrent_name}",
            dedup=True,
        )
        _replay_scan_events()
        return None, False

    heartbeat.checkpoint(force=True)
    if not begin_pack_queue_attachment(
        db,
        download_id,
        pack_reservation_owner,
        download_client_id=owner_id,
        protocol=normalized_protocol,
    ):
        remove_pack_queue_private_artifacts(
            download_id,
            pack_reservation_owner,
            download_client_id=owner_id,
            protocol=normalized_protocol,
        )
        release_pack_queue_creation(
            db,
            download_id,
            pack_reservation_owner,
            download_client_id=owner_id,
            protocol=normalized_protocol,
            commit=True,
            attaching=True,
        )
        return None, False

    if generated_pack_artifacts:
        try:
            durably_attach_pack_queue_directory(
                download_id,
                pack_reservation_owner,
                download_client_id=owner_id,
                protocol=normalized_protocol,
                checkpoint=heartbeat.checkpoint,
            )
        except OSError as exc:
            private_exists = os.path.lexists(private_pack_dir)
            canonical_exists = os.path.lexists(canonical_pack_dir)
            if private_exists:
                remove_pack_queue_private_artifacts(
                    download_id,
                    pack_reservation_owner,
                    download_client_id=owner_id,
                    protocol=normalized_protocol,
                )
            if private_exists or not canonical_exists:
                release_pack_queue_creation(
                    db,
                    download_id,
                    pack_reservation_owner,
                    download_client_id=owner_id,
                    protocol=normalized_protocol,
                    commit=True,
                    attaching=True,
                )
            log_event(
                "error",
                f"Import queue: could not publish generated pack for "
                f"{torrent_name}: {exc}",
                series_id,
                db=db,
                dedup=True,
            )
            return None, False
        file_rows = _canonicalize_pack_file_rows(
            file_rows,
            private_pack_dir,
            canonical_pack_dir,
        )

    heartbeat.checkpoint(force=True)
    if not refresh_pack_queue_creation(
        db,
        download_id,
        pack_reservation_owner,
        download_client_id=owner_id,
        protocol=normalized_protocol,
        lease_seconds=PACK_RESERVATION_SECONDS,
        commit=False,
    ):
        db.rollback()
        return None, False

    cur = db.execute(
        "INSERT INTO import_queue(series_id, download_id, download_client_id,"
        " download_protocol, torrent_name, torrent_url, volume_num, src_dir,"
        " status) VALUES(?,?,?,?,?,?,?,?,'pending')",
        (
            series_id,
            download_id,
            owner_id,
            normalized_protocol,
            torrent_name,
            torrent_url,
            volume_num,
            src_dir,
        ),
    )
    queue_id = cur.lastrowid

    db.executemany(
        "INSERT INTO import_queue_files"
        "(queue_id, filename, src_path, dst_path, proposed_volume, proposed_chapter,"
        " proposed_volume_range_start, proposed_volume_range_end,"
        " proposed_chapter_range_end, proposed_pack_type, proposed_is_special,"
        " proposed_import_kind, proposed_special_title, file_type, status)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(queue_id, *row) for row in file_rows],
    )
    release_pack_queue_creation(
        db,
        download_id,
        pack_reservation_owner,
        download_client_id=owner_id,
        protocol=normalized_protocol,
        commit=False,
        attaching=True,
    )

    needs_review = unmapped > 0 or special > 0
    if needs_review:
        reasons = []
        if unmapped:
            reasons.append(f"{unmapped} unmapped file(s)")
        if special:
            reasons.append(f"{special} special file(s)")
        log_event(
            "import",
            f"Queued for review ({', '.join(reasons)}): {torrent_name}",
            series_id,
            db=db,
        )
    _replay_scan_events()
    return queue_id, needs_review


def _canonicalize_pack_file_rows(
    file_rows: list[tuple[Any, ...]],
    private_pack_dir: str,
    canonical_pack_dir: str,
) -> list[tuple[Any, ...]]:
    canonical_rows: list[tuple[Any, ...]] = []
    private_abs = os.path.abspath(private_pack_dir)
    for row in file_rows:
        source_abs = os.path.abspath(str(row[1]))
        if os.path.commonpath((private_abs, source_abs)) != private_abs:
            canonical_rows.append(row)
            continue
        relative = os.path.relpath(source_abs, private_abs)
        canonical_source = os.path.join(canonical_pack_dir, relative)
        canonical_rows.append((row[0], canonical_source, *row[2:]))
    return canonical_rows


def _pack_image_dir_to_cbz(
    src_dir: str,
    dst_cbz: str,
    checkpoint: Callable[[], None],
) -> int | None:
    """Pack image pages cooperatively so reservation loss stops private writes."""
    try:
        pages: list[str] = []
        for name in sorted(os.listdir(src_dir)):
            checkpoint()
            source = os.path.join(src_dir, name)
            try:
                info = os.lstat(source)
            except OSError:
                continue
            if (
                os.path.splitext(name)[1].lower() in _IMAGE_EXTENSIONS
                and stat.S_ISREG(info.st_mode)
            ):
                pages.append(name)
        if not pages:
            return None

        os.makedirs(os.path.dirname(dst_cbz), exist_ok=True)
        with zipfile.ZipFile(dst_cbz, "w", zipfile.ZIP_STORED) as archive:
            for name in pages:
                checkpoint()
                source = os.path.join(src_dir, name)
                with open(source, "rb") as source_file:
                    with archive.open(name, "w") as destination:
                        while True:
                            chunk = source_file.read(1024 * 1024)
                            if not chunk:
                                break
                            checkpoint()
                            destination.write(chunk)
        checkpoint()
        return os.path.getsize(dst_cbz)
    except _PackQueueReservationLost:
        try:
            os.remove(dst_cbz)
        except FileNotFoundError:
            pass
        raise
    except Exception:
        try:
            os.remove(dst_cbz)
        except FileNotFoundError:
            pass
        return None


def _find_image_only_chapter_dirs(
    content_path: str,
    checkpoint: Callable[[], None],
) -> list[str]:
    """Find leaf directories containing only image files."""
    result: list[str] = []

    def _is_image_only_dir(dirpath: str) -> bool:
        try:
            files = os.listdir(dirpath)
            if not files:
                return False
            for f in files:
                checkpoint()
                ext = os.path.splitext(f)[1].lower()
                if ext and ext not in _IMAGE_EXTENSIONS:
                    return False
            return True
        except OSError:
            return False

    for root, dirs, files in os.walk(content_path):
        checkpoint()
        is_leaf = not dirs
        if is_leaf and _is_image_only_dir(root):
            result.append(root)

    return result


def _extract_zip_wrapped_split_rars(
    content_path: str,
    pack_dir: str,
    defer_event,
    heartbeat: _PackQueueHeartbeat,
) -> list[str] | None:
    """Extract scene-style ZIP wrapped split RAR payloads.

    Some DDL/tracker releases contain files like ``abc1.zip``/``abc2.zip``.
    Each outer ZIP contains one split RAR part (``abc.rar``, ``abc.r00``...),
    and the real manga payload is inside the reconstructed RAR. Treating the
    outer ZIPs as manga archives misclassifies opaque scene filenames as
    chapters, so queue the extracted payload instead.
    """
    checkpoint = heartbeat.checkpoint
    checkpoint()
    zip_paths: list[str] = []
    for name in sorted(os.listdir(content_path)):
        checkpoint()
        if name.lower().endswith(".zip"):
            zip_paths.append(os.path.join(content_path, name))
    if len(zip_paths) < 2:
        return None

    groups: dict[str, list[tuple[str, str]]] = {}
    for zip_path in zip_paths:
        checkpoint()
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    checkpoint()
                    if info.is_dir():
                        continue
                    member_name = os.path.basename(info.filename)
                    m = _SPLIT_RAR_PART_RE.match(member_name)
                    if not m:
                        continue
                    groups.setdefault(m.group("stem").lower(), []).append(
                        (zip_path, member_name)
                    )
        except _PackQueueReservationLost:
            raise
        except zipfile.BadZipFile:
            continue

    selected = [
        parts for parts in groups.values()
        if any(name.lower().endswith(".rar") for _, name in parts)
        and any(re.search(r"\.r\d{2}$", name, re.IGNORECASE) for _, name in parts)
    ]
    if not selected:
        return None

    split_root = os.path.join(pack_dir, "split-rar")
    if os.path.lexists(split_root):
        checkpoint()
        shutil.rmtree(split_root)
        checkpoint()
    os.makedirs(split_root, exist_ok=True)

    payloads: list[str] = []
    for idx, parts in enumerate(selected, start=1):
        checkpoint()
        group_dir = os.path.join(split_root, f"group-{idx}")
        out_dir = os.path.join(group_dir, "out")
        os.makedirs(group_dir, exist_ok=True)
        rar_path = None
        for zip_path, member_name in parts:
            checkpoint()
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    source_member = next(
                        info for info in zf.infolist()
                        if os.path.basename(info.filename) == member_name
                    )
                    target = os.path.join(group_dir, member_name)
                    with zf.open(source_member) as src, open(target, "wb") as dst:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            checkpoint()
                            dst.write(chunk)
                    if member_name.lower().endswith(".rar"):
                        rar_path = target
            except _PackQueueReservationLost:
                raise
            except Exception as exc:
                defer_event(
                    "error",
                    f"Split archive extract failed for {os.path.basename(zip_path)}: {exc}",
                    dedup=True,
                )
                return []

        if not rar_path:
            continue
        extractor = shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
        if extractor:
            archive_cmd = [extractor, "x", "-y", f"-o{out_dir}", rar_path]
        else:
            archive_cmd = ["unrar", "x", "-o+", rar_path, out_dir + os.sep]

        try:
            checkpoint()
            result = heartbeat.run(
                lambda: subprocess.run(
                    archive_cmd,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        except _PackQueueReservationLost:
            raise
        except Exception as exc:
            defer_event(
                "error",
                f"Split RAR unpack failed for {os.path.basename(rar_path)}: {exc}",
                dedup=True,
            )
            return []
        if getattr(result, "returncode", 0) != 0:
            detail = ((result.stderr or result.stdout or "").strip())[:500]
            suffix = f": {detail}" if detail else ""
            defer_event(
                "error",
                f"Split RAR unpack failed for {os.path.basename(rar_path)}{suffix}",
                dedup=True,
            )
            return []

        group_payloads: list[str] = []
        for root, dirs, files in os.walk(out_dir):
            checkpoint()
            dirs.sort()
            for fname in sorted(files):
                checkpoint()
                ext = os.path.splitext(fname)[1].lower()
                if ext in MANGA_EXTENSIONS:
                    group_payloads.append(os.path.join(root, fname))
        if not group_payloads:
            detail = ((result.stderr or result.stdout or "").strip())[:500]
            suffix = f": {detail}" if detail else ""
            defer_event(
                "error",
                f"Split RAR unpack produced no manga payloads for "
                f"{os.path.basename(rar_path)}{suffix}",
                dedup=True,
            )
            return []
        payloads.extend(group_payloads)

    if payloads:
        defer_event(
            "import",
            f"Unpacked {len(payloads)} ZIP-wrapped split RAR payload(s)",
        )
    return payloads
