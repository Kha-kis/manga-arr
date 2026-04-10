"""Suwayomi download client integration — GraphQL API (v2+).

Flow:
  1. find_or_add_manga()     → locate/add manga in Suwayomi library by MangaDex UUID
  2. fetch_chapters()        → pull chapter list from source
  3. suwayomi_grab()         → resolve volume→chapters, enqueue + start downloader
  4. check_suwayomi_jobs()   → poll isDownloaded on tracked chapters, mark complete
"""
import json
import logging
import os
import re

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from shared import get_cfg, get_db

log    = logging.getLogger(__name__)
router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _swy_base(c: dict) -> str:
    from routers.download_clients import client_base_url
    return client_base_url(c).rstrip('/')


def _auth(c: dict):
    u = c.get('username') or ''
    p = c.get('password') or ''
    return (u, p) if u else None


async def _gql(c: dict, query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL operation against Suwayomi and return data dict."""
    base = _swy_base(c)
    auth = _auth(c)
    async with httpx.AsyncClient(timeout=30, auth=auth,
                                  headers={"User-Agent": "Mangarr/1.0"}) as cli:
        r = await cli.post(
            f"{base}/api/graphql",
            json={"query": query, "variables": variables or {}},
        )
        r.raise_for_status()
        payload = r.json()
        if "errors" in payload:
            msgs = "; ".join(e.get("message", str(e)) for e in payload["errors"])
            raise RuntimeError(f"GraphQL: {msgs}")
        return payload.get("data") or {}


def get_suwayomi_client(db) -> dict | None:
    row = db.execute(
        "SELECT * FROM download_clients WHERE type='suwayomi' AND enabled=1"
        " ORDER BY priority, id LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


# ── Connection test ───────────────────────────────────────────────────────────

async def test_connection(c: dict) -> tuple[bool, str]:
    try:
        data = await _gql(c, "{ sources { nodes { id name lang } } }")
        nodes  = data.get("sources", {}).get("nodes", [])
        mdx    = [s for s in nodes if "mangadex" in s.get("name", "").lower()]
        return True, f"Connected · {len(nodes)} sources ({len(mdx)} MangaDex)"
    except Exception as e:
        return False, str(e)


# ── Source resolution ─────────────────────────────────────────────────────────

_SOURCE_CACHE: dict[str, str] = {}   # lang → source_id

async def get_mangadex_source_id(c: dict, lang: str = "en") -> str | None:
    """Return the Suwayomi source ID for MangaDex in the requested language."""
    cache_key = f"{_swy_base(c)}:{lang}"
    if cache_key in _SOURCE_CACHE:
        return _SOURCE_CACHE[cache_key]

    data   = await _gql(c, "{ sources { nodes { id name lang } } }")
    nodes  = data.get("sources", {}).get("nodes", [])

    # Exact match first
    for s in nodes:
        if "mangadex" in s.get("name", "").lower() and s.get("lang", "") == lang:
            _SOURCE_CACHE[cache_key] = s["id"]
            return s["id"]
    # Fall back to English
    for s in nodes:
        if "mangadex" in s.get("name", "").lower() and s.get("lang", "") == "en":
            _SOURCE_CACHE[f"{_swy_base(c)}:en"] = s["id"]
            return s["id"]
    return None


# ── Manga library management ──────────────────────────────────────────────────

async def find_or_add_manga(c: dict, mangadex_uuid: str,
                             title: str, lang: str = "en") -> int:
    """
    Return Suwayomi manga ID for the given MangaDex UUID.
    Checks library first; if not found, searches source and adds to library.
    """
    # 1. Already in library?
    data = await _gql(c, "{ mangas(condition: {inLibrary: true}) { nodes { id url } } }")
    for m in data.get("mangas", {}).get("nodes", []):
        if mangadex_uuid in (m.get("url") or ""):
            return int(m["id"])

    # 2. Search source
    source_id = await get_mangadex_source_id(c, lang)
    if not source_id:
        raise RuntimeError(f"No MangaDex source for lang={lang!r}")

    data = await _gql(c, """
        mutation($src: LongString!, $q: String!, $p: Int!) {
            fetchSourceManga(input: {source: $src, type: SEARCH, query: $q, page: $p}) {
                mangas { id url }
            }
        }
    """, {"src": source_id, "q": title, "p": 1})

    manga_id = None
    for m in (data.get("fetchSourceManga") or {}).get("mangas") or []:
        if mangadex_uuid in (m.get("url") or ""):
            manga_id = int(m["id"])
            break

    if manga_id is None:
        raise RuntimeError(
            f"Manga {title!r} (uuid={mangadex_uuid}) not found in Suwayomi source"
        )

    # 3. Add to library
    await _gql(c, """
        mutation($id: Int!) {
            updateManga(input: {id: $id, patch: {inLibrary: true}}) { clientMutationId }
        }
    """, {"id": manga_id})

    return manga_id


# ── Chapter management ────────────────────────────────────────────────────────

async def fetch_chapters(c: dict, manga_id: int) -> list[dict]:
    """Fetch/refresh chapters from source. Returns full chapter list."""
    data = await _gql(c, """
        mutation($mid: Int!) {
            fetchChapters(input: {mangaId: $mid}) {
                chapters {
                    id chapterNumber name sourceOrder isDownloaded
                }
            }
        }
    """, {"mid": manga_id})
    return (data.get("fetchChapters") or {}).get("chapters") or []


def _vol_from_name(name: str | None) -> float | None:
    """Extract volume number from chapter name like 'Vol.1 Ch.2 - Title'."""
    m = re.search(r'Vol\.(\d+(?:\.\d+)?)', name or "", re.IGNORECASE)
    return float(m.group(1)) if m else None


def _chapters_for_volume(chapters: list[dict],
                          volume_num: float,
                          series_id: int | None = None) -> list[dict]:
    """
    Filter chapters belonging to a given volume.
    Primary:  parse 'Vol.X' from chapter name.
    Fallback: look up mangadex_chapters table to get chapter_num ranges per volume.
    """
    matched = [ch for ch in chapters
               if (v := _vol_from_name(ch.get("name"))) is not None
               and abs(v - volume_num) < 0.1]
    if matched:
        return matched

    # Fallback: use mangadex_chapters to know which chapter numbers belong to vol
    if series_id is None:
        return []

    with get_db() as db:
        rows = db.execute(
            "SELECT chapter_num FROM mangadex_chapters WHERE series_id=? AND volume_num=?",
            (series_id, volume_num)
        ).fetchall()

    if not rows:
        return []

    ch_nums = {float(r["chapter_num"]) for r in rows if r["chapter_num"] is not None}
    return [ch for ch in chapters
            if ch.get("chapterNumber") is not None
            and float(ch["chapterNumber"]) in ch_nums]


# ── Filesystem helpers ───────────────────────────────────────────────────────

def _swy_library_base(c: dict) -> str | None:
    """Return the host-visible path to Suwayomi's download root directory.
    Configured via the download_path field on the Suwayomi client.
    e.g.  Suwayomi container sees  /manga
          Mangarr container sees    /data/media/manga  → user sets this
    """
    return (c.get("download_path") or "").strip() or None


def _find_suwayomi_manga_dir(c: dict, title: str) -> str | None:
    """Find the host-visible download directory for a manga title.
    Structure: {library_base}/mangas/{source_name}/{manga_title}/
    """
    base = _swy_library_base(c)
    if not base:
        return None
    mangas_root = os.path.join(base, "mangas")
    if not os.path.isdir(mangas_root):
        return None
    for source_dir in os.listdir(mangas_root):
        manga_dir = os.path.join(mangas_root, source_dir, title)
        if os.path.isdir(manga_dir):
            return manga_dir
    return None


def _ch_sort_key(path: str) -> float:
    m = re.search(r"Ch\.(\d+(?:\.\d+)?)", os.path.basename(path), re.IGNORECASE)
    return float(m.group(1)) if m else 9999


def _vol_chapter_cbzs(manga_dir: str, volume_num: float) -> list[str]:
    """Return sorted list of chapter CBZ paths belonging to a volume."""
    vol_int = int(volume_num)
    paths = [
        os.path.join(manga_dir, fname)
        for fname in os.listdir(manga_dir)
        if fname.lower().endswith(".cbz")
        and re.search(rf"Vol\.{vol_int}\b", fname, re.IGNORECASE)
    ]
    return sorted(paths, key=_ch_sort_key)


def _chapter_cbz(manga_dir: str, chapter_num: float) -> str | None:
    """Find the CBZ file for a specific chapter number in manga_dir."""
    ch_int = int(chapter_num)
    frac   = chapter_num - ch_int
    # Match Ch.N or Ch.N.M (e.g. Ch.5, Ch.5.5)
    pattern = rf"Ch\.{ch_int}(?:\.\d+)?\b" if frac == 0 else rf"Ch\.{chapter_num}"
    for fname in os.listdir(manga_dir):
        if not fname.lower().endswith(".cbz"):
            continue
        if re.search(pattern, fname, re.IGNORECASE):
            return os.path.join(manga_dir, fname)
    return None


def _merge_cbzs(chapter_paths: list[str], output_path: str) -> int:
    """Merge ordered chapter CBZ files into a single volume CBZ.
    Returns total byte size of the output file, or 0 on failure.
    """
    import zipfile as _zf
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    page = 1
    try:
        with _zf.ZipFile(output_path, "w", _zf.ZIP_STORED) as out:
            for ch_path in chapter_paths:
                with _zf.ZipFile(ch_path, "r") as ch:
                    images = sorted([
                        n for n in ch.namelist()
                        if os.path.splitext(n)[1].lower()
                        in (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")
                        and not os.path.basename(n).startswith(".")
                    ])
                    for img in images:
                        ext = os.path.splitext(img)[1]
                        with ch.open(img) as f:
                            out.writestr(f"{page:04d}{ext}", f.read())
                        page += 1
        return os.path.getsize(output_path)
    except Exception as e:
        log.error("_merge_cbzs → %s: %s", output_path, e)
        try:
            os.remove(output_path)
        except OSError:
            pass
        return 0


# ── Main grab entry point ─────────────────────────────────────────────────────

def _ddl_enabled() -> bool:
    """Return False if the user has turned DDL off globally."""
    return get_cfg("ddl_grab_mode", "fallback") != "off"


async def suwayomi_grab(series_id: int, volume_num: float) -> bool:
    """
    Queue a volume download via Suwayomi DDL.
    Returns True if queued successfully (or already queued).
    """
    import main as _m

    if not _ddl_enabled():
        return False

    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        c = get_suwayomi_client(db)
        # Duplicate guard — don't re-queue an already-active job
        existing = db.execute(
            "SELECT id FROM suwayomi_downloads"
            " WHERE series_id=? AND volume_num=? AND status IN ('queued','error')",
            (series_id, volume_num),
        ).fetchone()

    if not c or not s:
        return False
    if not s["mangadex_id"]:
        log.debug("Series %d has no mangadex_id — skipping DDL", series_id)
        return False
    if existing:
        log.debug("suwayomi_grab: active job already exists for series %d vol %s", series_id, volume_num)
        return True   # already in queue, treat as success

    lang = s["ddl_language"] or get_cfg("ddl_language", "en")

    try:
        manga_id  = await find_or_add_manga(c, s["mangadex_id"], s["title"], lang)
        chapters  = await fetch_chapters(c, manga_id)
        vol_chs   = _chapters_for_volume(chapters, volume_num, series_id)

        if not vol_chs:
            log.warning("No chapters found in Suwayomi for series %d vol %s", series_id, volume_num)
            return False

        chapter_ids = [ch["id"] for ch in vol_chs]

        await _gql(c, """
            mutation($ids: [Int!]!) {
                enqueueChapterDownloads(input: {ids: $ids}) { clientMutationId }
            }
        """, {"ids": chapter_ids})
        await _gql(c, "mutation { startDownloader(input: {}) { clientMutationId } }")

        with get_db() as db:
            db.execute(
                "UPDATE volumes SET status='grabbed', grabbed_at=CURRENT_TIMESTAMP,"
                " client='suwayomi'"
                " WHERE series_id=? AND volume_num=? AND status='wanted'",
                (series_id, volume_num),
            )
            cur = db.execute(
                "INSERT INTO suwayomi_downloads"
                "(series_id, volume_num, suwayomi_manga_id, chapter_ids, status, total)"
                " VALUES(?,?,?,?,?,?)",
                (series_id, volume_num, manga_id,
                 json.dumps(chapter_ids), "queued", len(chapter_ids)),
            )
            db.execute("UPDATE series SET suwayomi_id=? WHERE id=?", (manga_id, series_id))

        _m.log_event(
            "grab",
            f"DDL queued: {s['title']} vol {volume_num} ({len(chapter_ids)} chapters via Suwayomi)",
            series_id,
        )
        return True

    except Exception as e:
        log.error("suwayomi_grab series=%d vol=%s: %s", series_id, volume_num, e, exc_info=True)
        return False


async def suwayomi_chapter_grab(series_id: int, chapter_num: float) -> bool:
    """Queue a single chapter download via Suwayomi DDL for ongoing/uncollected chapters."""
    import main as _m

    if not _ddl_enabled():
        return False

    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        c = get_suwayomi_client(db)
        existing = db.execute(
            "SELECT id FROM suwayomi_downloads"
            " WHERE series_id=? AND chapter_num=? AND status IN ('queued','error')",
            (series_id, chapter_num),
        ).fetchone()

    if not c or not s:
        return False
    if not s["mangadex_id"]:
        log.debug("Series %d has no mangadex_id — skipping DDL chapter grab", series_id)
        return False
    if existing:
        log.debug("suwayomi_chapter_grab: active job already exists for series %d ch %s",
                  series_id, chapter_num)
        return True

    lang = s["ddl_language"] or get_cfg("ddl_language", "en")

    try:
        manga_id = await find_or_add_manga(c, s["mangadex_id"], s["title"], lang)
        chapters = await fetch_chapters(c, manga_id)

        matched = [ch for ch in chapters
                   if ch.get("chapterNumber") is not None
                   and abs(float(ch["chapterNumber"]) - chapter_num) < 0.01]
        if not matched:
            log.warning("Chapter %.3g not found in Suwayomi for series %d", chapter_num, series_id)
            return False

        chapter_ids = [matched[0]["id"]]

        await _gql(c, """
            mutation($ids: [Int!]!) {
                enqueueChapterDownloads(input: {ids: $ids}) { clientMutationId }
            }
        """, {"ids": chapter_ids})
        await _gql(c, "mutation { startDownloader(input: {}) { clientMutationId } }")

        with get_db() as db:
            db.execute(
                "UPDATE chapters SET status='grabbed', grabbed_at=CURRENT_TIMESTAMP,"
                " client='suwayomi'"
                " WHERE series_id=? AND chapter_num=? AND status='wanted'",
                (series_id, chapter_num),
            )
            db.execute(
                "INSERT INTO suwayomi_downloads"
                "(series_id, chapter_num, suwayomi_manga_id, chapter_ids, status, total)"
                " VALUES(?,?,?,?,?,?)",
                (series_id, chapter_num, manga_id,
                 json.dumps(chapter_ids), "queued", 1),
            )
            db.execute("UPDATE series SET suwayomi_id=? WHERE id=?", (manga_id, series_id))

        _m.log_event(
            "grab",
            f"DDL queued: {s['title']} ch {chapter_num} via Suwayomi",
            series_id,
        )
        return True

    except Exception as e:
        log.error("suwayomi_chapter_grab series=%d ch=%s: %s",
                  series_id, chapter_num, e, exc_info=True)
        return False


# ── Import completed DDL download into managed library ────────────────────────

def _should_merge(c: dict) -> bool:
    """Return True if chapter CBZs should be merged into a single volume CBZ."""
    return bool(c.get("merge_chapters", 1))


async def _import_suwayomi_volume(c: dict, series_id: int, volume_num: float
                                  ) -> tuple[str | None, int]:
    """Import completed volume download into the managed library.
    If merge_chapters is enabled (default): merges chapter CBZs into one volume CBZ.
    If disabled: copies individual chapter CBZs to a volume subdirectory.
    Returns (import_path, size_bytes).
    """
    import main as _m

    with get_db() as db:
        s_row      = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        series_dir = _m._series_library_dir(db, series_id) if s_row else None

    if not s_row:
        return None, 0

    manga_dir = _find_suwayomi_manga_dir(c, s_row["title"])
    if not manga_dir:
        log.warning("Suwayomi library base not configured — skipping import series %d vol %s",
                    series_id, volume_num)
        return None, 0

    chapter_paths = _vol_chapter_cbzs(manga_dir, volume_num)
    if not chapter_paths:
        log.warning("No chapter CBZs found for series %d vol %s in %s",
                    series_id, volume_num, manga_dir)
        return None, 0

    safe_title = _m.sanitize_filename(s_row["title"])
    vol_str    = str(int(volume_num)).zfill(2) if volume_num == int(volume_num) else str(volume_num)

    if _should_merge(c):
        # ── Merge all chapters into one volume CBZ ────────────────────────────
        out_name = f"{safe_title} v{vol_str}.cbz"
        out_path = os.path.join(series_dir, out_name) if series_dir else None
        if not out_path:
            return None, 0
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path, os.path.getsize(out_path)
        log.info("Merging %d chapter(s) → %s", len(chapter_paths), out_path)
        size = _merge_cbzs(chapter_paths, out_path)
        return (out_path, size) if size else (None, 0)
    else:
        # ── Copy individual chapter CBZs to a volume subdirectory ─────────────
        vol_dir = os.path.join(series_dir, f"v{vol_str}") if series_dir else None
        if not vol_dir:
            return None, 0
        os.makedirs(vol_dir, exist_ok=True)
        total_size = 0
        import shutil
        for src in chapter_paths:
            dst = os.path.join(vol_dir, os.path.basename(src))
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            total_size += os.path.getsize(dst)
        # Return the directory path as import_path when not merging
        return (vol_dir, total_size) if total_size else (None, 0)


async def _import_suwayomi_chapter(c: dict, series_id: int, chapter_num: float
                                   ) -> tuple[str | None, int]:
    """Import a single downloaded chapter CBZ into the managed library.
    Individual chapters are always kept as individual files (merge doesn't apply).
    Returns (import_path, size_bytes).
    """
    import main as _m

    with get_db() as db:
        s_row      = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        series_dir = _m._series_library_dir(db, series_id) if s_row else None

    if not s_row:
        return None, 0

    manga_dir = _find_suwayomi_manga_dir(c, s_row["title"])
    if not manga_dir:
        return None, 0

    src = _chapter_cbz(manga_dir, chapter_num)
    if not src:
        log.warning("Chapter CBZ not found for series %d ch %s in %s",
                    series_id, chapter_num, manga_dir)
        return None, 0

    if not series_dir:
        return src, os.path.getsize(src)   # keep in Suwayomi dir if no library configured

    safe_title = _m.sanitize_filename(s_row["title"])
    ch_str     = str(int(chapter_num)).zfill(3) if chapter_num == int(chapter_num) else str(chapter_num)
    out_name   = f"{safe_title} Ch{ch_str}.cbz"
    out_path   = os.path.join(series_dir, out_name)

    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path, os.path.getsize(out_path)

    import shutil
    os.makedirs(series_dir, exist_ok=True)
    shutil.copy2(src, out_path)
    return out_path, os.path.getsize(out_path)


# ── Background job checker ────────────────────────────────────────────────────

async def check_suwayomi_jobs():
    """
    Called periodically from main.check_download_status().
    Polls each active suwayomi_downloads job and marks complete when done.
    """
    import main as _m

    with get_db() as db:
        jobs = db.execute(
            "SELECT * FROM suwayomi_downloads WHERE status='queued'"
        ).fetchall()
        if not jobs:
            return
        c = get_suwayomi_client(db)

    if not c:
        return

    for job in jobs:
        try:
            chapter_ids = json.loads(job["chapter_ids"])

            # Fetch chapters for the manga and filter to our tracked IDs
            data  = await _gql(c, """
                query($mid: Int!) {
                    manga(id: $mid) {
                        chapters { nodes { id isDownloaded } }
                    }
                }
            """, {"mid": job["suwayomi_manga_id"]})

            ch_map: dict[int, bool] = {
                int(ch["id"]): bool(ch["isDownloaded"])
                for ch in (data.get("manga") or {}).get("chapters", {}).get("nodes") or []
            }
            done = sum(1 for cid in chapter_ids if ch_map.get(cid, False))

            with get_db() as db:
                db.execute(
                    "UPDATE suwayomi_downloads SET progress=? WHERE id=?",
                    (done, job["id"]),
                )

            if done >= len(chapter_ids):
                if job["chapter_num"] is not None:
                    # ── Chapter-level job ─────────────────────────────────────
                    import_path, file_bytes = await _import_suwayomi_chapter(
                        c, job["series_id"], float(job["chapter_num"])
                    )
                    if not import_path:
                        err_msg = "Import failed — chapter CBZ not found in library path"
                        with get_db() as db:
                            db.execute(
                                "UPDATE suwayomi_downloads SET status='error', error=? WHERE id=?",
                                (err_msg, job["id"]),
                            )
                        log.error("DDL chapter import failed: series=%d ch=%s",
                                  job["series_id"], job["chapter_num"])
                        continue
                    with get_db() as db:
                        db.execute(
                            "UPDATE suwayomi_downloads SET status='completed' WHERE id=?",
                            (job["id"],),
                        )
                        db.execute(
                            "UPDATE chapters SET status='downloaded',"
                            " imported_at=CURRENT_TIMESTAMP, client='suwayomi',"
                            " import_path=?, quality=COALESCE(quality,?),"
                            " size_bytes=COALESCE(NULLIF(size_bytes,0),?),"
                            " indexer=NULL, protocol=NULL, torrent_name=NULL,"
                            " torrent_url=NULL, download_id=NULL, release_group=NULL"
                            " WHERE series_id=? AND chapter_num=?"
                            " AND status IN ('grabbed','wanted','downloaded')",
                            (import_path, _m.quality_from_filename(import_path),
                             file_bytes or None,
                             job["series_id"], job["chapter_num"]),
                        )
                    _m.log_event(
                        "download_complete",
                        f"DDL imported: series {job['series_id']} ch {job['chapter_num']}"
                        + (f" → {os.path.basename(import_path)}" if import_path else ""),
                        job["series_id"],
                    )
                else:
                    # ── Volume-level job ──────────────────────────────────────
                    import_path, file_bytes = await _import_suwayomi_volume(
                        c, job["series_id"], job["volume_num"]
                    )
                    if not import_path:
                        err_msg = "Import failed — CBZ files not found in library path"
                        with get_db() as db:
                            db.execute(
                                "UPDATE suwayomi_downloads SET status='error', error=? WHERE id=?",
                                (err_msg, job["id"]),
                            )
                        log.error("DDL volume import failed: series=%d vol=%s",
                                  job["series_id"], job["volume_num"])
                        continue
                    with get_db() as db:
                        db.execute(
                            "UPDATE suwayomi_downloads SET status='completed' WHERE id=?",
                            (job["id"],),
                        )
                        db.execute(
                            "UPDATE volumes SET status='downloaded',"
                            " imported_at=CURRENT_TIMESTAMP,"
                            " client='suwayomi', import_path=?,"
                            " quality=COALESCE(quality,?),"
                            " size_bytes=COALESCE(NULLIF(size_bytes,0),?),"
                            " indexer=NULL, protocol=NULL, torrent_name=NULL,"
                            " download_id=NULL, release_group=NULL"
                            " WHERE series_id=? AND volume_num=?"
                            " AND status IN ('grabbed','wanted','downloaded')",
                            (import_path, _m.quality_from_filename(import_path),
                             file_bytes or None,
                             job["series_id"], job["volume_num"]),
                        )
                    _m.log_event(
                        "download_complete",
                        f"DDL imported: series {job['series_id']} vol {job['volume_num']}"
                        + (f" → {os.path.basename(import_path)}" if import_path else ""),
                        job["series_id"],
                    )

        except Exception as e:
            log.error("check_suwayomi_jobs job=%d: %s", job["id"], e)
            # Mark the job as errored so it doesn't loop forever on every poll.
            try:
                with get_db() as db:
                    db.execute(
                        "UPDATE suwayomi_downloads SET status='error', error=? WHERE id=?",
                        (f"{type(e).__name__}: {e}"[:500], job["id"]),
                    )
            except Exception:
                pass  # don't let error-path DB issues kill the loop


# ── Monitoring loop ───────────────────────────────────────────────────────────

async def _suwayomi_sync_series(c: dict, s: dict) -> tuple[int, int]:
    """
    Sync one series against Suwayomi's live chapter feed.
    - Grabs wanted volumes whose chapters are now available.
    - Grabs wanted uncollected chapters (volume_id IS NULL) now available.
    - Discovers chapters Suwayomi knows about that have no stub yet → creates them.
    Returns (volumes_grabbed, chapters_grabbed).
    """
    import main as _m

    lang = s.get("ddl_language") or get_cfg("ddl_language", "en")

    manga_id = await find_or_add_manga(c, s["mangadex_id"], s["title"], lang)
    chapters = await fetch_chapters(c, manga_id)

    if not chapters:
        return 0, 0

    # Build a map of chapter_num → suwayomi chapter for fast lookup
    swy_by_num: dict[float, dict] = {}
    for ch in chapters:
        cn = ch.get("chapterNumber")
        if cn is not None:
            try:
                swy_by_num[float(cn)] = ch
            except (TypeError, ValueError):
                pass

    if not swy_by_num:
        return 0, 0

    vol_grabbed = ch_grabbed = 0
    series_id   = s["id"]

    with get_db() as db:
        # ── 1. Discover new chapters ───────────────────────────────────────────
        # Only for actively-releasing series — finished series won't get new chapters
        if (s.get("status") or "").upper() in ("RELEASING", "HIATUS"):
            existing_nums = {
                float(r["chapter_num"])
                for r in db.execute(
                    "SELECT chapter_num FROM chapters WHERE series_id=?", (series_id,)
                ).fetchall()
            }
            vol_id_map: dict[float, int] = {
                float(r["volume_num"]): r["id"]
                for r in db.execute(
                    "SELECT id, volume_num FROM volumes"
                    " WHERE series_id=? AND volume_num IS NOT NULL",
                    (series_id,)
                ).fetchall()
            }
            try:
                ch_vol_map: dict = json.loads(s["chapter_vol_map"]) if s.get("chapter_vol_map") else {}
            except Exception:
                ch_vol_map = {}

            newly_created = 0
            for ch_num, swy_ch in swy_by_num.items():
                if ch_num in existing_nums:
                    continue
                # Determine volume assignment
                vol_num = (ch_vol_map.get(str(int(ch_num))) if ch_num == int(ch_num)
                           else ch_vol_map.get(str(ch_num)))
                if vol_num is None:
                    # Try extracting from the Suwayomi chapter name (e.g. "Vol.3 Ch.21")
                    vol_num = _vol_from_name(swy_ch.get("name"))
                if vol_num is not None:
                    vol_num = float(vol_num)
                vol_id = vol_id_map.get(vol_num) if vol_num is not None else None

                cur = db.execute(
                    "INSERT OR IGNORE INTO chapters"
                    "(series_id, volume_id, chapter_num, status, monitored)"
                    " VALUES(?,?,?,'wanted',1)",
                    (series_id, vol_id, ch_num),
                )
                if cur.rowcount:
                    newly_created += 1
            if newly_created:
                log.info("suwayomi_monitor series %d: discovered %d new chapter(s)",
                         series_id, newly_created)

        # ── 2. Wanted volumes ──────────────────────────────────────────────────
        wanted_vols = db.execute(
            "SELECT volume_num FROM volumes WHERE series_id=? AND status='wanted'"
            " AND monitored=1 AND volume_num IS NOT NULL",
            (series_id,),
        ).fetchall()

        # ── 3. Wanted uncollected chapters ────────────────────────────────────
        wanted_chs = db.execute(
            "SELECT chapter_num FROM chapters WHERE series_id=? AND status='wanted'"
            " AND monitored=1 AND volume_id IS NULL",
            (series_id,),
        ).fetchall()

    # Grab wanted volumes whose chapters are available in Suwayomi
    for row in wanted_vols:
        vol_num  = float(row["volume_num"])
        vol_chs  = _chapters_for_volume(chapters, vol_num, series_id)
        if vol_chs:
            ok = await suwayomi_grab(series_id, vol_num)
            if ok:
                vol_grabbed += 1

    # Grab wanted uncollected chapters available in Suwayomi
    for row in wanted_chs:
        ch_num = float(row["chapter_num"])
        if ch_num in swy_by_num:
            ok = await suwayomi_chapter_grab(series_id, ch_num)
            if ok:
                ch_grabbed += 1

    return vol_grabbed, ch_grabbed


async def suwayomi_monitor_loop():
    """
    Periodic loop: scan all monitored series and auto-grab wanted items via Suwayomi DDL.

    Interval is controlled by the 'suwayomi_check_interval' setting (seconds, default 6h).
    Only runs when a Suwayomi client is configured and enabled.

    What it does each pass:
      1. For RELEASING/HIATUS series: detects new chapters from Suwayomi's source feed
         and creates chapter stubs in the DB for any not yet tracked.
      2. For all monitored series: grabs wanted volumes when their chapters are available.
      3. For all monitored series: grabs wanted uncollected chapters when available.
    """
    import asyncio as _aio
    import main as _m

    await _aio.sleep(180)   # startup delay — let other loops and DB init settle
    while True:
        try:
            if not _ddl_enabled():
                interval = max(3600, int(get_cfg("suwayomi_check_interval", "21600")))
                await _aio.sleep(interval)
                continue

            with get_db() as db:
                c = get_suwayomi_client(db)

            if c:
                with get_db() as db:
                    candidates = db.execute(
                        "SELECT id, title, mangadex_id, ddl_language,"
                        " chapter_vol_map, status"
                        " FROM series WHERE monitored=1 AND mangadex_id IS NOT NULL"
                    ).fetchall()

                vol_total = ch_total = 0
                for row in candidates:
                    try:
                        vg, cg = await _suwayomi_sync_series(c, dict(row))
                        vol_total += vg
                        ch_total  += cg
                    except Exception as e:
                        log.warning("suwayomi_monitor series %d (%s): %s",
                                    row["id"], row["title"], e)
                    await _aio.sleep(3)   # rate-limit: ~20 series/min

                if vol_total or ch_total:
                    _m.log_event(
                        "suwayomi_sync",
                        f"Suwayomi monitor: {vol_total} vol(s), {ch_total} chapter(s) queued",
                    )
        except Exception as e:
            log.error("suwayomi_monitor_loop: %s", e, exc_info=True)

        interval = max(3600, int(get_cfg("suwayomi_check_interval", "21600")))
        await _aio.sleep(interval)


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.get("/api/suwayomi/sources")
async def list_sources():
    with get_db() as db:
        c = get_suwayomi_client(db)
    if not c:
        return JSONResponse({"ok": False, "message": "No Suwayomi client configured"})
    try:
        data    = await _gql(c, "{ sources { nodes { id name lang } } }")
        sources = data.get("sources", {}).get("nodes", [])
        return JSONResponse({"ok": True, "sources": sources})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)})


@router.post("/api/suwayomi/jobs/{job_id}/retry")
async def retry_suwayomi_job(job_id: int):
    """Reset a failed DDL job back to queued so it will be retried on the next poll."""
    with get_db() as db:
        job = db.execute(
            "SELECT * FROM suwayomi_downloads WHERE id=? AND status='error'", (job_id,)
        ).fetchone()
        if not job:
            return JSONResponse({"ok": False, "message": "Job not found or not in error state"},
                                status_code=404)
        db.execute(
            "UPDATE suwayomi_downloads SET status='queued', error=NULL, progress=0 WHERE id=?",
            (job_id,),
        )
        # Reset volume/chapter back to grabbed so the completion UPDATE will match
        if job["volume_num"] is not None:
            db.execute(
                "UPDATE volumes SET status='grabbed' WHERE series_id=? AND volume_num=?"
                " AND status NOT IN ('downloaded','grabbed')",
                (job["series_id"], job["volume_num"]),
            )
        elif job["chapter_num"] is not None:
            db.execute(
                "UPDATE chapters SET status='grabbed' WHERE series_id=? AND chapter_num=?"
                " AND status NOT IN ('downloaded','grabbed')",
                (job["series_id"], job["chapter_num"]),
            )
    return JSONResponse({"ok": True})


@router.post("/api/suwayomi/jobs/{job_id}/cancel")
async def cancel_suwayomi_job(job_id: int):
    """Cancel a queued/errored DDL job and reset the volume/chapter back to wanted."""
    with get_db() as db:
        job = db.execute(
            "SELECT * FROM suwayomi_downloads WHERE id=? AND status IN ('queued','error')",
            (job_id,)
        ).fetchone()
        if not job:
            return JSONResponse({"ok": False, "message": "Job not found or already completed"},
                                status_code=404)
        db.execute("DELETE FROM suwayomi_downloads WHERE id=?", (job_id,))
        if job["volume_num"] is not None:
            db.execute(
                "UPDATE volumes SET status='wanted', client=NULL, grabbed_at=NULL"
                " WHERE series_id=? AND volume_num=? AND status='grabbed' AND client='suwayomi'",
                (job["series_id"], job["volume_num"]),
            )
        elif job["chapter_num"] is not None:
            db.execute(
                "UPDATE chapters SET status='wanted', client=NULL, grabbed_at=NULL"
                " WHERE series_id=? AND chapter_num=? AND status='grabbed' AND client='suwayomi'",
                (job["series_id"], job["chapter_num"]),
            )
    return JSONResponse({"ok": True})


@router.post("/api/series/{series_id}/suwayomi/match")
async def match_series_endpoint(series_id: int):
    with get_db() as db:
        s = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        c = get_suwayomi_client(db)
    if not c or not s:
        return JSONResponse({"ok": False, "message": "Not found"}, status_code=404)
    try:
        lang     = s["ddl_language"] or get_cfg("ddl_language", "en")
        manga_id = await find_or_add_manga(c, s["mangadex_id"], s["title"], lang)
        with get_db() as db:
            db.execute("UPDATE series SET suwayomi_id=? WHERE id=?", (manga_id, series_id))
        return JSONResponse({"ok": True, "suwayomi_id": manga_id})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@router.post("/api/series/{series_id}/suwayomi/grab/{volume_num:float}")
async def grab_volume_endpoint(series_id: int, volume_num: float):
    ok = await suwayomi_grab(series_id, volume_num)
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "message": "DDL grab failed — check logs"}, status_code=500)


@router.get("/api/suwayomi/downloads")
async def list_downloads():
    with get_db() as db:
        rows = db.execute(
            "SELECT sd.*, s.title FROM suwayomi_downloads sd"
            " JOIN series s ON s.id=sd.series_id"
            " ORDER BY sd.created_at DESC LIMIT 100"
        ).fetchall()
    return JSONResponse({"downloads": [dict(r) for r in rows]})
