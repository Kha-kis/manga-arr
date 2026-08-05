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
import hashlib
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from events import log_event
from parsing import normalize
from shared import get_cfg, get_db


DownloadClientLoadReason = Literal[
    "matched",
    "deleted",
    "disabled",
    "identity_mismatch",
]


@dataclass(frozen=True, slots=True)
class BoundDownloadClient:
    """Credentials loaded only after a journaled client identity matches."""

    client: dict[str, Any] | None
    reason: DownloadClientLoadReason


@dataclass(frozen=True, slots=True, eq=False)
class GrabResult:
    """Download handoff result with exact, non-secret client ownership.

    Iteration intentionally preserves the historical four-value unpacking
    contract. New callers should read ``download_client_id`` directly.
    """

    success: bool
    client_name: str
    download_id: str | None
    client_healthy: bool
    download_client_id: int | None

    def _legacy_tuple(self) -> tuple[bool, str, str | None, bool]:
        return (
            self.success,
            self.client_name,
            self.download_id,
            self.client_healthy,
        )

    def __iter__(self) -> Iterator[bool | str | None]:
        yield from self._legacy_tuple()

    def __len__(self) -> int:
        return 4

    def __getitem__(self, index: int) -> bool | str | None:
        return self._legacy_tuple()[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, GrabResult):
            return (
                self._legacy_tuple() == other._legacy_tuple()
                and self.download_client_id == other.download_client_id
            )
        if isinstance(other, tuple):
            return self._legacy_tuple() == other
        return False


def load_bound_download_client(
    client_id: int,
    *,
    expected_type: str,
    expected_name: str,
) -> BoundDownloadClient:
    """Load one exact client without falling back to another configured server."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM download_clients WHERE id=?",
            (client_id,),
        ).fetchone()
    if row is None:
        return BoundDownloadClient(None, "deleted")

    raw = dict(row)
    if int(raw.get("enabled") or 0) != 1:
        return BoundDownloadClient(None, "disabled")
    if (
        str(raw.get("type") or "") != expected_type
        or str(raw.get("name") or "") != expected_name
    ):
        return BoundDownloadClient(None, "identity_mismatch")

    # Identity is checked before the encrypted password is decrypted.
    from routers.download_clients import client_base_url
    from security import decrypt_secret_safe

    client = raw
    client["password"] = decrypt_secret_safe(
        client.get("password"),
        field_name="download_clients.password",
        context=expected_name,
    )
    client["host"] = client_base_url(client)
    return BoundDownloadClient(client, "matched")


def extract_magnet_hash(magnet: str) -> str | None:
    """Return the 40-hex (or 32-base-32) hash from a magnet URI, or None."""
    m = re.search(
        r"xt=urn:btih:([0-9a-fA-F]{40}|[0-9a-zA-Z]{32})",
        magnet,
        re.IGNORECASE,
    )
    return m.group(1).lower() if m else None


def _skip_bencoded_value(data: bytes, offset: int, depth: int = 0) -> int:
    """Return the offset immediately after one bounded bencoded value."""
    if depth > 100 or offset >= len(data):
        raise ValueError("invalid bencoded value")

    marker = data[offset]
    if marker == ord("i"):
        end = data.find(b"e", offset + 1)
        if end < 0 or end == offset + 1:
            raise ValueError("invalid bencoded integer")
        encoded_integer = data[offset + 1 : end]
        digits = (
            encoded_integer[1:] if encoded_integer.startswith(b"-") else encoded_integer
        )
        if not digits or not all(ord("0") <= byte <= ord("9") for byte in digits):
            raise ValueError("invalid bencoded integer")
        if (len(digits) > 1 and digits.startswith(b"0")) or encoded_integer == b"-0":
            raise ValueError("non-canonical bencoded integer")
        return end + 1

    if marker in {ord("l"), ord("d")}:
        cursor = offset + 1
        is_dict = marker == ord("d")
        while cursor < len(data) and data[cursor] != ord("e"):
            if is_dict:
                cursor = _skip_bencoded_string(data, cursor)[1]
            cursor = _skip_bencoded_value(data, cursor, depth + 1)
        if cursor >= len(data):
            raise ValueError("unterminated bencoded collection")
        return cursor + 1

    if ord("0") <= marker <= ord("9"):
        return _skip_bencoded_string(data, offset)[1]

    raise ValueError("invalid bencoded marker")


def _skip_bencoded_string(data: bytes, offset: int) -> tuple[bytes, int]:
    """Return a bencoded byte string and the following offset."""
    colon = data.find(b":", offset)
    if colon < 0:
        raise ValueError("invalid bencoded string")
    length_bytes = data[offset:colon]
    if not length_bytes or not all(
        ord("0") <= byte <= ord("9") for byte in length_bytes
    ):
        raise ValueError("invalid bencoded string length")
    if len(length_bytes) > 1 and length_bytes.startswith(b"0"):
        raise ValueError("non-canonical bencoded string length")
    length = int(length_bytes)
    value_start = colon + 1
    value_end = value_start + length
    if value_end > len(data):
        raise ValueError("truncated bencoded string")
    return data[value_start:value_end], value_end


@dataclass(frozen=True, slots=True)
class _TorrentInfoIdentity:
    qbit_hash: str
    lookup_hashes: tuple[str, ...]


def _torrent_info_identity(torrent_bytes: bytes) -> _TorrentInfoIdentity | None:
    """Derive qBittorrent-compatible IDs from the exact raw ``info`` value."""
    try:
        if not torrent_bytes or torrent_bytes[0] != ord("d"):
            return None
        cursor = 1
        info_span: tuple[int, int] | None = None
        previous_key: bytes | None = None
        while cursor < len(torrent_bytes) and torrent_bytes[cursor] != ord("e"):
            key, cursor = _skip_bencoded_string(torrent_bytes, cursor)
            if previous_key is not None and key <= previous_key:
                return None
            previous_key = key
            value_start = cursor
            cursor = _skip_bencoded_value(torrent_bytes, cursor, 1)
            if key == b"info":
                if info_span is not None or torrent_bytes[value_start] != ord("d"):
                    return None
                info_span = (value_start, cursor)
        if cursor >= len(torrent_bytes) or cursor + 1 != len(torrent_bytes):
            return None
        if info_span is None:
            return None
        start, end = info_span

        info_cursor = start + 1
        info_keys: set[bytes] = set()
        meta_version: int | None = None
        previous_info_key: bytes | None = None
        while info_cursor < end - 1:
            key, info_cursor = _skip_bencoded_string(torrent_bytes, info_cursor)
            if previous_info_key is not None and key <= previous_info_key:
                return None
            previous_info_key = key
            info_keys.add(key)

            value_start = info_cursor
            info_cursor = _skip_bencoded_value(torrent_bytes, info_cursor, 2)
            if key == b"meta version":
                if torrent_bytes[value_start] != ord("i"):
                    return None
                meta_version = int(torrent_bytes[value_start + 1 : info_cursor - 1])

        if info_cursor != end - 1:
            return None

        raw_info = torrent_bytes[start:end]
        v1_hash = hashlib.sha1(raw_info, usedforsecurity=False).hexdigest()
        if meta_version is None:
            return _TorrentInfoIdentity(v1_hash, (v1_hash,))
        if meta_version != 2 or b"file tree" not in info_keys:
            return None

        # qBittorrent's TorrentID is 160 bits. libtorrent's get_best() uses a
        # truncated v2 hash when present, including for hybrid torrents.
        v2_hash = hashlib.sha256(raw_info, usedforsecurity=False).digest()[:20].hex()
        lookup_hashes = (v2_hash, v1_hash) if b"pieces" in info_keys else (v2_hash,)
        return _TorrentInfoIdentity(v2_hash, lookup_hashes)
    except (ValueError, OverflowError):
        return None


def _torrent_info_hash(torrent_bytes: bytes) -> str | None:
    """Return the 40-hex ID qBittorrent exposes through its Web API."""
    identity = _torrent_info_identity(torrent_bytes)
    return identity.qbit_hash if identity is not None else None


async def _find_qbit_torrent_hash(
    http_client: httpx.AsyncClient,
    host: str,
    category: str,
    *,
    expected_hashes: tuple[str, ...],
    torrent_name: str | None,
    add_failed: bool,
) -> tuple[str | None, bool]:
    """Poll qBit after an add and return ``(hash, lookup_was_healthy)``."""
    normalized_name = normalize(torrent_name) if torrent_name else ""
    add_time = time.time()
    lookup_was_healthy = False

    for sleep_s, use_category, limit in (
        (1.5, True, 10),
        (2.0, False, 30),
    ):
        await asyncio.sleep(sleep_s)
        if expected_hashes:
            params: dict[str, str | int | bool] = {"hashes": "|".join(expected_hashes)}
        else:
            params = {"filter": "all"}
            if use_category or add_failed:
                params["category"] = category
            if not add_failed:
                params.update({"sort": "added_on", "reverse": "true", "limit": limit})
        try:
            response = await http_client.get(
                f"{host}/api/v2/torrents/info", params=params
            )
        except httpx.RequestError:
            continue
        if response.status_code != 200:
            continue

        lookup_was_healthy = True
        payload = response.json()
        if not isinstance(payload, list):
            continue
        torrents = [torrent for torrent in payload if isinstance(torrent, dict)]
        if expected_hashes:
            for torrent in torrents:
                reported_hash = str(torrent.get("hash") or "").lower()
                if reported_hash in expected_hashes:
                    return reported_hash, True
            continue

        for torrent in torrents:
            reported_name = normalize(str(torrent.get("name") or ""))
            if normalized_name and (
                normalized_name == reported_name
                or normalized_name in reported_name
                or reported_name in normalized_name
            ):
                reported_hash = str(torrent.get("hash") or "").lower()
                if reported_hash:
                    return reported_hash, True
        if not normalized_name and not add_failed and torrents:
            newest = torrents[0]
            added_on = newest.get("added_on")
            if (
                isinstance(added_on, (int, float))
                and not isinstance(added_on, bool)
                and time.time() - added_on < add_time + sleep_s + 1
            ):
                reported_hash = str(newest.get("hash") or "").lower()
                if reported_hash:
                    return reported_hash, True

    return None, lookup_was_healthy


async def qbit_grab(
    torrent_url: str,
    client: dict[str, Any] | None = None,
    save_path: str | None = None,
    torrent_name: str | None = None,
) -> tuple[bool, str | None, bool]:
    """Add to qBittorrent. Returns (success, torrent_hash_or_None, client_healthy).

    ``client_healthy`` is True when auth + add succeeded, or when an ambiguous
    add request was followed by a healthy qBit lookup. Used by the circuit
    breaker so routine matching failures do not trip it.
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
            magnet_hash = extract_magnet_hash(torrent_url)
            expected_hashes: tuple[str, ...] = (magnet_hash,) if magnet_hash else ()
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
                        identity = _torrent_info_identity(tf.content)
                        expected_hashes = identity.lookup_hashes if identity else ()
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

            add_response: httpx.Response | None = None
            add_request_error: httpx.RequestError | None = None
            try:
                if add_files:
                    add_response = await hc.post(
                        f"{host}/api/v2/torrents/add", data=add_data, files=add_files
                    )
                else:
                    add_response = await hc.post(
                        f"{host}/api/v2/torrents/add", data=add_data
                    )
            except httpx.RequestError as exc:
                # A response timeout or connection loss after writing the body
                # is ambiguous: qBit may have accepted the torrent. Reconcile
                # below instead of immediately reporting a hard failure.
                add_request_error = exc

            if add_request_error is None:
                assert add_response is not None
                if add_response.status_code != 200:
                    return False, None, False
                add_failed = add_response.text.strip() == "Fails."
            else:
                add_failed = False

            dl_id = magnet_hash if torrent_url.startswith("magnet:") else None

            lookup_was_healthy = True
            if not dl_id or add_request_error is not None:
                dl_id, lookup_was_healthy = await _find_qbit_torrent_hash(
                    hc,
                    host,
                    cat,
                    expected_hashes=expected_hashes,
                    torrent_name=torrent_name,
                    # An ambiguous request must match by exact hash or name;
                    # never claim an unrelated concurrently-added "newest" row.
                    add_failed=add_failed or add_request_error is not None,
                )

            if not dl_id:
                if add_request_error is not None:
                    detail = (
                        str(add_request_error).strip()
                        or type(add_request_error).__name__
                    )
                    log_event(
                        "error",
                        f"[qBit] add request {detail}; no matching torrent found",
                    )
                else:
                    log_event(
                        "error",
                        f"[qBit] grab added but hash not found for: {torrent_name!r}",
                    )
                return False, None, lookup_was_healthy

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


async def qbit_remove(
    download_id: str,
    delete_files: bool = False,
    *,
    client: dict[str, Any] | None = None,
) -> bool:
    """Remove a torrent from qBittorrent by hash. Returns True on success."""
    if not download_id:
        return False

    _c = client
    if _c is None:
        from routers.download_clients import get_client_for_protocol

        with get_db() as _rdb:
            _c = get_client_for_protocol(_rdb, "torrent")
    if not _c:
        return False
    host = (_c.get("host") or "").rstrip("/")
    user = _c.get("username") or ""
    pw = _c.get("password") or ""
    try:
        async with httpx.AsyncClient(timeout=10) as http_client:
            r = await http_client.post(
                f"{host}/api/v2/auth/login", data={"username": user, "password": pw}
            )
            if "Ok" not in r.text:
                return False
            r2 = await http_client.post(
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


async def sab_remove(
    nzo_id: str,
    *,
    client: dict[str, Any] | None = None,
) -> bool:
    """Remove a completed job from SABnzbd. Returns True on success."""
    if not nzo_id:
        return False

    _c = client
    if _c is None:
        from routers.download_clients import get_client_for_protocol

        with get_db() as _rdb:
            _c = get_client_for_protocol(_rdb, "nzb")
    if not _c:
        return False
    host = (_c.get("host") or "").rstrip("/")
    apikey = _c.get("password") or ""
    try:
        async with httpx.AsyncClient(timeout=10) as http_client:
            r = await http_client.get(
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
        log_event(
            "configuration_error",
            "[SAB] API key is not configured",
            dedup=True,
        )
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
) -> GrabResult:
    """Route to best available download client.

    Returns a :class:`GrabResult`. Its legacy four-value iteration is
    ``(success, client_name, download_id, client_healthy)``; the exact selected
    database identity is available as ``download_client_id``.

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
        return GrabResult(False, "none", None, False, None)

    raw_client_id = client.get("id")
    client_id = (
        raw_client_id
        if isinstance(raw_client_id, int)
        and not isinstance(raw_client_id, bool)
        and raw_client_id > 0
        else None
    )
    breaker_client_id = client_id or 0
    if _cb_is_open(breaker_client_id):
        log_event(
            "error",
            f"[grab_url] Circuit open for client {client['name']} — skipping grab",
            dedup=True,
        )
        return GrabResult(False, client["name"], None, False, client_id)

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
        return GrabResult(False, client["name"], None, False, client_id)

    if healthy:
        _cb_record_success(breaker_client_id)
    else:
        _cb_record_failure(breaker_client_id)
    return GrabResult(
        ok,
        (client.get("type") or client["name"]).lower(),
        dl_id,
        healthy,
        client_id,
    )
