"""Validated, atomic remote and CBZ cover caching.

All covers are normalized to JPEG because the public cover URL has a stable
``.jpg`` suffix. Existing covers are preserved unless a complete replacement
has been decoded and written successfully.
"""
from io import BytesIO
import hashlib
import os
import tempfile
import zipfile

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from events import log_event

# Ensure the covers dir exists at import time. If the container
# filesystem isn't writable (tests that redirect /config via
# conftest's makedirs redirect), the caller's redirected path is
# created instead — behaviour matches the pre-split code at
# main.py:8640 which ran this at module top level.
COVERS_DIR = "/config/covers"
MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_SOURCE_PIXELS = 40_000_000
MAX_COVER_SIZE = (2400, 3600)
os.makedirs(COVERS_DIR, exist_ok=True)


def _cover_path(series_id: int) -> str:
    return os.path.join(COVERS_DIR, f"{series_id}.jpg")


def _normalize_cover(data: bytes) -> tuple[bytes, str]:
    """Decode a bounded image payload and return browser-safe JPEG bytes."""
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f"cover payload must be 1-{MAX_SOURCE_BYTES} bytes")

    try:
        with Image.open(BytesIO(data)) as source:
            source_format = (source.format or "unknown").lower()
            if source_format not in {"jpeg", "png", "webp"}:
                raise ValueError(f"unsupported cover format: {source_format}")
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_SOURCE_PIXELS:
                raise ValueError(f"cover dimensions are not allowed: {width}x{height}")
            source.load()
            image = ImageOps.exif_transpose(source)
            image.thumbnail(MAX_COVER_SIZE, Image.Resampling.LANCZOS)

            if image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                flattened = Image.new("RGB", rgba.size, "white")
                flattened.paste(rgba, mask=rgba.getchannel("A"))
                image = flattened
            else:
                image = image.convert("RGB")

            output = BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=90,
                optimize=True,
                progressive=True,
            )
            return output.getvalue(), source_format
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"invalid cover image: {exc}") from exc


def _atomic_write_cover(dest: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".cover-", dir=os.path.dirname(dest))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cached_cover_is_valid(path: str) -> bool:
    """Return whether a cache entry is a decodable JPEG within size limits."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False
        with Image.open(path) as image:
            width, height = image.size
            if image.format != "JPEG" or width * height > MAX_SOURCE_PIXELS:
                return False
            image.verify()
        return True
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ):
        return False


async def download_cover(
    series_id: int, cover_url: str, *, force: bool = False
) -> dict:
    """Download, validate, normalize, and atomically cache a series cover.

    follow_redirects=False: a public hostname could 30x to a private
    IP and bypass validate_outbound_url. AniList/MangaDex serve
    covers from direct CDN URLs, so disabling redirects has no
    practical cost.
    """
    if not cover_url:
        return {"ok": False, "status": "missing_url"}
    dest = _cover_path(series_id)
    if not force and cached_cover_is_valid(dest):
        return {"ok": True, "status": "cached", "path": dest}
    from security import validate_outbound_url, UnsafeURLError
    try:
        validate_outbound_url(cover_url)
    except UnsafeURLError as e:
        log_event("error", f"[Cover] URL rejected for series {series_id}: {e}", series_id)
        return {"ok": False, "status": "rejected", "error": str(e)}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            async with client.stream("GET", cover_url) as response:
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                try:
                    declared_size = int(content_length) if content_length else None
                except (TypeError, ValueError):
                    declared_size = None
                if declared_size is not None and declared_size > MAX_SOURCE_BYTES:
                    raise ValueError(
                        f"cover response exceeds {MAX_SOURCE_BYTES} bytes"
                    )
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > MAX_SOURCE_BYTES:
                        raise ValueError(
                            f"cover response exceeds {MAX_SOURCE_BYTES} bytes"
                        )
                    payload.extend(chunk)
            data = bytes(payload)
            data, source_format = _normalize_cover(data)

        old_hash = None
        if os.path.exists(dest):
            old_hash = _sha256_file(dest)
        new_hash = hashlib.sha256(data).hexdigest()
        if old_hash != new_hash:
            _atomic_write_cover(dest, data)
        return {
            "ok": True,
            "status": "updated" if old_hash != new_hash else "unchanged",
            "path": dest,
            "format": "jpeg",
            "source_format": source_format,
            "bytes": len(data),
            "sha256": new_hash,
        }
    except ValueError as exc:
        error = str(exc)
        log_event("error", f"[Cover] {error} for series {series_id}", series_id)
        return {"ok": False, "status": "invalid_image", "error": error}
    except Exception as e:
        log_event("error", f"[Cover] download error for series {series_id}: {e}", series_id)
        return {"ok": False, "status": "download_failed", "error": str(e)[:300]}


def extract_cbz_cover(series_id: int, cbz_path: str) -> bool:
    """Normalize and cache the first supported image from a CBZ."""
    dest = _cover_path(series_id)
    if cached_cover_is_valid(dest):
        return True
    try:
        with zipfile.ZipFile(cbz_path, 'r') as z:
            images = sorted([f for f in z.namelist()
                           if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
                           and not f.startswith('__MACOSX')])
            if images:
                with z.open(images[0]) as img_file:
                    source = img_file.read(MAX_SOURCE_BYTES + 1)
                data, _source_format = _normalize_cover(source)
                _atomic_write_cover(dest, data)
                return True
    except Exception as e:
        log_event("error", f"[Cover] CBZ extract error: {e}", series_id)
    return False
