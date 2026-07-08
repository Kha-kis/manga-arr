"""Download-client adapters: qBittorrent, SABnzbd, NZBGet, blackhole.

Third module extracted from main.py. Each adapter:
  - Takes the decrypted download_clients row as a dict
  - Returns (ok: bool, download_id: str|None, client_healthy: bool)

`client_healthy=False` means the client itself was unreachable / failed
auth (trip the circuit breaker). `client_healthy=True` with `ok=False`
means the client was healthy but the add was rejected at the
business level (no CB trip).

The high-level dispatcher `grab_url()` picks the right adapter based
on protocol + configured clients, routes the call, and records the
CB result.

Pure move from main.py — no behaviour changes.
"""

from __future__ import annotations

import asyncio
import os
import re
import time

import httpx

from events import log_event
from parsing import normalize
from shared import get_cfg, get_db


def extract_magnet_hash(magnet: str) -> str | None:
    """Return the 40-hex (or 32-base-32) hash from a magnet URI, or None."""
    m = re.search(
        r"xt=urn:btih:([0-9a-fA-F]{40}|[0-9a-zA-Z]{32})",
        magnet,
        re.IGNORECASE,
    )
    return m.group(1).lower() if m else None


async def qbit_grab(
    torrent_url: str,
    client: dict | None = None,
    save_path: str | None = None,
    torrent_name: str | None = None,
) -> tuple[bool, str | None, bool]:
    """Add to qBittorrent. Returns (success, torrent_hash_or_None, client_healthy).

    ``client_healthy`` is True when auth + add succeeded, even if the hash
    couldn't be matched afterwards. Used by the circuit breaker so we don't
    trip it on routine matching failures (qBit was reachable the whole time).
    """
    _cfg = client or {}
    host = (_cfg.get("host") or "").rstrip("/")
    user = _cfg.get("username") or ""
    pw = _cfg.get("password") or ""
    cat = _cfg.get("category") or get_cfg("category")
    _state = _cfg.get("initial_state") or "normal"
    _seq = bool(_cfg.get("sequential_order"))
    _flf = bool(_cfg.get("first_last_first"))
    _layout = _cfg.get("content_layout") or "original"
    try:
        async with httpx.AsyncClient(timeout=20) as hc:
            r = await hc.post(
                f"{host}/api/v2/auth/login", data={"username": user, "password": pw}
            )
            if "Ok" not in r.text:
                # Auth fail = real client-health problem → trip CB
                return False, None, False

            # For non-magnet URLs, pre-fetch the .torrent file from within the
            # container (where Prowlarr/indexer URLs are reachable) and upload
            # the raw bytes to qBit. Avoids qBit trying to fetch Docker-internal
            # hostnames from its VPN namespace.
            add_files = None
            add_data = {"category": cat}
            if save_path:
                add_data["savepath"] = save_path
            if _state == "paused":
                add_data["paused"] = "true"
            if _seq:
                add_data["sequentialDownload"] = "true"
            if _flf:
                add_data["firstLastPiecePrio"] = "true"
            _layout_map = {"subfolder": "Subfolder", "none": "NoSubfolder"}
            if _layout in _layout_map:
                add_data["contentLayout"] = _layout_map[_layout]

            if torrent_url.startswith("magnet:"):
                add_data["urls"] = torrent_url
            else:
                try:
                    tf = await hc.get(torrent_url, follow_redirects=True, timeout=15)
                    if tf.status_code == 200 and tf.content:
                        add_files = {
                            "torrents": (
                                "upload.torrent",
                                tf.content,
                                "application/x-bittorrent",
                            )
                        }
                    else:
                        add_data["urls"] = torrent_url  # fallback
                except Exception:
                    add_data["urls"] = torrent_url  # fallback

            if add_files:
                r2 = await hc.post(
                    f"{host}/api/v2/torrents/add", data=add_data, files=add_files
                )
            else:
                r2 = await hc.post(f"{host}/api/v2/torrents/add", data=add_data)

            if r2.status_code != 200:
                return False, None, False
            add_failed = r2.text.strip() == "Fails."

            dl_id = (
                extract_magnet_hash(torrent_url)
                if torrent_url.startswith("magnet:")
                else None
            )

            if not dl_id:
                norm_name = normalize(torrent_name) if torrent_name else ""
                add_time = time.time()

                for attempt, (sleep_s, use_cat, limit) in enumerate(
                    [
                        (1.5, True, 10),  # pass 1: fast, category-scoped
                        (2.0, False, 30),  # pass 2: slower, all categories
                    ]
                ):
                    await asyncio.sleep(sleep_s)
                    params: dict = {"filter": "all"}
                    if use_cat or add_failed:
                        params["category"] = cat
                    if not add_failed:
                        params.update(
                            {"sort": "added_on", "reverse": "true", "limit": limit}
                        )
                    r3 = await hc.get(f"{host}/api/v2/torrents/info", params=params)
                    if r3.status_code == 200:
                        for t in r3.json():
                            t_norm = normalize(t.get("name", ""))
                            if norm_name and (
                                norm_name == t_norm
                                or norm_name in t_norm
                                or t_norm in norm_name
                            ):
                                dl_id = t.get("hash", "").lower() or None
                                break
                        if not dl_id and not norm_name and not add_failed and r3.json():
                            newest = r3.json()[0]
                            if (
                                time.time() - newest.get("added_on", 0)
                                < add_time + sleep_s + 1
                            ):
                                dl_id = newest.get("hash", "").lower() or None
                    if dl_id:
                        break

            if not dl_id:
                log_event("error", f"[qBit] grab added but hash not found for: {torrent_name!r}")
                return False, None, True

            if _state == "forced" and dl_id:
                try:
                    await hc.post(
                        f"{host}/api/v2/torrents/setForceStart",
                        data={"hashes": dl_id, "value": "true"},
                    )
                except Exception:
                    pass

            return True, dl_id, True
    except Exception as e:
        log_event("error", f"[qBit] grab error: {e}")
        return False, None, False


async def qbit_remove(download_id: str, delete_files: bool = False) -> bool:
    """Remove a torrent from qBittorrent by hash. Returns True on success."""
    if not download_id:
        return False
    from routers.download_clients import get_client_for_protocol

    with get_db() as _rdb:
        _c = get_client_for_protocol(_rdb, "torrent")
    if not _c:
        return False
    host = (_c.get("host") or "").rstrip("/")
    user = _c.get("username") or ""
    pw = _c.get("password") or ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{host}/api/v2/auth/login", data={"username": user, "password": pw}
            )
            if "Ok" not in r.text:
                return False
            r2 = await client.post(
                f"{host}/api/v2/torrents/delete",
                data={
                    "hashes": download_id,
                    "deleteFiles": "true" if delete_files else "false",
                },
            )
            return r2.status_code == 200
    except Exception as e:
        log_event("error", f"[qBit] remove error: {e}")
        return False


async def sab_remove(nzo_id: str) -> bool:
    """Remove a completed job from SABnzbd. Returns True on success."""
    if not nzo_id:
        return False
    from routers.download_clients import get_client_for_protocol

    with get_db() as _rdb:
        _c = get_client_for_protocol(_rdb, "nzb")
    if not _c:
        return False
    host = (_c.get("host") or "").rstrip("/")
    apikey = _c.get("password") or ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{host}/api",
                params={
                    "mode": "history",
                    "action": "delete",
                    "del_files": "0",
                    "value": nzo_id,
                    "apikey": apikey,
                    "output": "json",
                },
            )
            return r.status_code == 200
    except Exception as e:
        log_event("error", f"[SAB] remove error: {e}")
        return False


async def sab_grab(
    nzb_url: str, client: dict | None = None, save_path: str | None = None
) -> tuple[bool, str | None, bool]:
    """Add to SABnzbd. Returns (success, nzo_id_or_None, client_healthy)."""
    host = ((client or {}).get("host") or "").rstrip("/")
    apikey = (client or {}).get("password") or ""
    cat = (client or {}).get("category") or get_cfg("category")
    if not apikey:
        return False, None, False
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post(
                f"{host}/api",
                params={
                    "mode": "addurl",
                    "name": nzb_url,
                    "cat": cat,
                    "apikey": apikey,
                    "output": "json",
                },
            )
            data = r.json()
            if data.get("status") is True:
                nzo_ids = data.get("nzo_ids", [])
                return (True, nzo_ids[0], True) if nzo_ids else (False, None, True)
            return False, None, True
    except Exception as e:
        log_event("error", f"[SAB] grab error: {e}")
        return False, None, False


async def nzbget_grab(
    nzb_url: str, client: dict | None = None
) -> tuple[bool, str | None, bool]:
    """Add to NZBGet via JSON-RPC. Returns (success, nzb_id_or_None, client_healthy)."""
    host = ((client or {}).get("host") or "").rstrip("/")
    user = (client or {}).get("username") or ""
    pw = (client or {}).get("password") or ""
    cat = (client or {}).get("category") or get_cfg("category")
    port = (client or {}).get("port") or 6789
    api_url = f"http://{user}:{pw}@{host}:{port}/jsonrpc"
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post(
                api_url,
                json={
                    "method": "append",
                    "params": [nzb_url, cat, 0, False, "", 0, "SCORE"],
                },
            )
            data = r.json()
            nzb_id = data.get("result")
            if nzb_id and nzb_id > 0:
                return True, str(nzb_id), True
            return False, None, True
    except Exception as e:
        log_event("error", f"[NZBGet] grab error: {e}")
        return False, None, False


async def blackhole_grab(
    url: str, client: dict, torrent_name: str | None = None
) -> tuple[bool, str | None, bool]:
    """Download a .torrent file and drop it in the blackhole folder.
    Returns (success, dl_id, client_healthy)."""
    folder = (client.get("host") or "").strip()
    if not folder or not os.path.isdir(folder):
        log_event("error", f"[Blackhole] Folder not found: {folder!r}")
        return False, None, False
    fname = (torrent_name or "download") + ".torrent"
    fname = re.sub(r'[<>:"/\\|?*]', "_", fname)
    dest = os.path.join(folder, fname)
    try:
        if url.startswith("magnet:"):
            dest = dest.replace(".torrent", ".magnet")
            with open(dest, "w") as f:
                f.write(url)
        else:
            async with httpx.AsyncClient(timeout=20) as cli:
                r = await cli.get(url, follow_redirects=True)
                if r.status_code != 200:
                    return False, None, True
            with open(dest, "wb") as f:
                f.write(r.content)
        return True, os.path.basename(dest), True
    except Exception as e:
        log_event("error", f"[Blackhole] grab error: {e}")
        return False, None, False


async def grab_url(
    url: str,
    protocol: str = "",
    save_path: str | None = None,
    torrent_name: str | None = None,
    series_id: int | None = None,
) -> tuple[bool, str, str | None, bool]:
    """Route to best available download client.

    Returns (success, client_name, download_id, client_healthy).

      success         — True iff the grab fully succeeded (added AND
                        the download_id is known).
      client_name     — adapter type / client name for accounting.
      download_id     — qBit hash / SAB nzo_id, or None if unknown.
      client_healthy  — True if the client itself worked (auth + add
                        succeeded). Distinguishes "qBit accepted the
                        torrent but Mangarr couldn't find its hash"
                        (success=False, healthy=True) from "qBit
                        unreachable / auth fail" (both False).

    The healthy flag both drives the circuit breaker (existing) AND
    lets the caller insert `seen` for URL-dedup even on the soft-failure
    path, preventing the infinite RSS-retry loop where qBit keeps adding
    duplicate copies of the same torrent because the dedup never fires.
    """
    use_torrent = (
        protocol == "torrent" or url.endswith(".torrent") or url.startswith("magnet:")
    )
    detected_protocol = "torrent" if use_torrent else "nzb"

    from routers.download_clients import (
        get_client_for_protocol,
        _cb_is_open,
        _cb_record_success,
        _cb_record_failure,
    )

    series_tags: list[str] = []
    if series_id:
        with get_db() as _tdb:
            series_tags = [
                r["tag"]
                for r in _tdb.execute(
                    "SELECT tag FROM series_tags WHERE series_id=?", (series_id,)
                ).fetchall()
            ]
    with get_db() as _tdb:
        client = get_client_for_protocol(_tdb, detected_protocol, series_tags)

    if not client:
        log_event("error", f"[grab_url] No download client configured for {detected_protocol}")
        return False, "none", None, False

    client_id = client.get("id", 0) or 0
    if _cb_is_open(client_id):
        log_event("error", f"[grab_url] Circuit open for client {client['name']} — skipping grab")
        return False, client["name"], None, False

    ctype = client["type"]
    if ctype == "qbittorrent":
        ok, dl_id, healthy = await qbit_grab(
            url, client=client, save_path=save_path, torrent_name=torrent_name
        )
    elif ctype == "sabnzbd":
        ok, dl_id, healthy = await sab_grab(url, client=client, save_path=save_path)
    elif ctype == "blackhole":
        ok, dl_id, healthy = await blackhole_grab(
            url, client=client, torrent_name=torrent_name
        )
    elif ctype == "nzbget":
        ok, dl_id, healthy = await nzbget_grab(url, client=client)
    else:
        log_event("error", f"[grab_url] Client type '{ctype}' not yet implemented")
        return False, client["name"], None, False

    if healthy:
        _cb_record_success(client_id)
    else:
        _cb_record_failure(client_id)
    return ok, (client.get("type") or client["name"]).lower(), dl_id, healthy
