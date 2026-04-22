"""Cover image helpers: download from URL, extract from CBZ.

First module extracted from main.py as part of the structural split.
Pure move — no behaviour changes. main.py re-exports the symbols so
existing call sites (`main.download_cover`, `main.extract_cbz_cover`,
and `_m.download_cover` via `import main as _m` in routers) keep
working unchanged.

Covers are stored under /config/covers/{series_id}.jpg. Existing
files are never overwritten by either helper — the series editor is
the explicit path for replacing a cover.
"""
import os
import zipfile

import httpx

# Ensure the covers dir exists at import time. If the container
# filesystem isn't writable (tests that redirect /config via
# conftest's makedirs redirect), the caller's redirected path is
# created instead — behaviour matches the pre-split code at
# main.py:8640 which ran this at module top level.
os.makedirs('/config/covers', exist_ok=True)


async def download_cover(series_id: int, cover_url: str) -> None:
    """Download cover from URL and save to /config/covers/{series_id}.jpg.

    No-op if the destination already exists (covers are never auto-
    overwritten — the series editor is the explicit replace path).
    Exits silently on network errors, bad status codes, or URL
    validation failure; cover absence is not a critical failure and
    the series page falls back to a placeholder.

    follow_redirects=False: a public hostname could 30x to a private
    IP and bypass validate_outbound_url. AniList/MangaDex serve
    covers from direct CDN URLs, so disabling redirects has no
    practical cost.
    """
    if not cover_url:
        return
    dest = f"/config/covers/{series_id}.jpg"
    if os.path.exists(dest):
        return  # already have a cover
    from security import validate_outbound_url, UnsafeURLError
    try:
        validate_outbound_url(cover_url)
    except UnsafeURLError as e:
        print(f"[Cover] URL rejected for series {series_id}: {e}")
        return
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            r = await client.get(cover_url)
            if r.status_code == 200:
                with open(dest, 'wb') as f:
                    f.write(r.content)
    except Exception as e:
        print(f"[Cover] download error for series {series_id}: {e}")


def extract_cbz_cover(series_id: int, cbz_path: str) -> None:
    """Extract the first image from a CBZ and save it as the series cover.

    No-op if the destination already exists. Skips __MACOSX entries.
    Silent on any zip read / extraction error — the caller is
    fire-and-forget from the import pipeline and cover absence is
    non-critical.
    """
    dest = f"/config/covers/{series_id}.jpg"
    if os.path.exists(dest):
        return
    try:
        with zipfile.ZipFile(cbz_path, 'r') as z:
            images = sorted([f for f in z.namelist()
                           if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
                           and not f.startswith('__MACOSX')])
            if images:
                with z.open(images[0]) as img_file:
                    with open(dest, 'wb') as out:
                        out.write(img_file.read())
    except Exception as e:
        print(f"[Cover] CBZ extract error: {e}")
