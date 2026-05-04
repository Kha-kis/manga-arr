"""Tests for auto-packing image-only chapter directories into CBZs.

Production observation: torrents like
  /data/torrents/manga/One Piece - Ch. 991 [VIZ] [Digital] [amit34521]/
contain raw page images (001.jpg, 002.jpg, ...) instead of a CBZ
archive. Mangarr's import scanner only matches MANGA_EXTENSIONS
(.cbz/.cbr/.zip/...), so these produced "No manga files found"
indefinitely — one path generated 207,162 spam events before PR #145
deduped the log. Even with the dedup, the underlying chapter never
got imported.

The fix: detect leaf directories with only images (no archives, no
subdirs) and pack each into a CBZ in a staging area, then point the
import scanner at the packed CBZ. The user's library gets the
chapter as a CBZ; the source torrent dir is untouched.
"""
import os
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


# ───────────────────── find_image_only_chapter_dirs ─────────────────────


def test_finds_flat_image_only_dir(tmp_path):
    """A flat directory with only image files is a leaf to pack."""
    from files import find_image_only_chapter_dirs

    d = tmp_path / "Series - Ch. 991"
    d.mkdir()
    for i in range(1, 6):
        (d / f"{i:03d}.jpg").write_bytes(b"fake-jpeg-data")

    leafs = find_image_only_chapter_dirs(str(tmp_path))
    assert leafs == [str(d)], f"expected single leaf, got {leafs}"


def test_skips_dir_with_archive(tmp_path):
    """If the directory already has a CBZ/CBR, skip it — the existing
    archive covers the chapter; packing images would duplicate."""
    from files import find_image_only_chapter_dirs

    d = tmp_path / "Series Vol 1"
    d.mkdir()
    (d / "Series Vol 1.cbz").write_bytes(b"PK\x03\x04...")
    (d / "001.jpg").write_bytes(b"jpg")

    leafs = find_image_only_chapter_dirs(str(tmp_path))
    assert leafs == [], "dir with archive must be skipped"


def test_skips_empty_dir(tmp_path):
    """Empty leaf dirs aren't candidates."""
    from files import find_image_only_chapter_dirs
    (tmp_path / "Empty").mkdir()
    assert find_image_only_chapter_dirs(str(tmp_path)) == []


def test_walks_nested_chapter_dirs(tmp_path):
    """Multi-chapter torrents put each chapter in its own subdir.
    Each leaf with images becomes a separate pack candidate."""
    from files import find_image_only_chapter_dirs

    base = tmp_path / "Series Vol 1-2"
    (base / "Ch. 001").mkdir(parents=True)
    (base / "Ch. 002").mkdir(parents=True)
    (base / "Ch. 003").mkdir(parents=True)
    for ch_dir in (base / "Ch. 001", base / "Ch. 002", base / "Ch. 003"):
        (ch_dir / "001.jpg").write_bytes(b"jpg")
        (ch_dir / "002.jpg").write_bytes(b"jpg")

    leafs = sorted(find_image_only_chapter_dirs(str(tmp_path)))
    assert len(leafs) == 3
    assert all('Ch. 00' in l for l in leafs)


def test_skips_thumbnail_metadata_files(tmp_path):
    """Junk metadata (Thumbs.db, .DS_Store, ComicInfo.xml) doesn't
    disqualify an image-only dir from being a pack candidate."""
    from files import find_image_only_chapter_dirs

    d = tmp_path / "Ch. 100"
    d.mkdir()
    (d / "001.jpg").write_bytes(b"jpg")
    (d / "Thumbs.db").write_bytes(b"junk")
    (d / ".DS_Store").write_bytes(b"junk")
    (d / "ComicInfo.xml").write_bytes(b"<ComicInfo/>")

    leafs = find_image_only_chapter_dirs(str(tmp_path))
    assert leafs == [str(d)]


def test_recognizes_multiple_image_extensions(tmp_path):
    """JPG, PNG, WEBP, GIF, BMP, AVIF all qualify as page images."""
    from files import find_image_only_chapter_dirs
    d = tmp_path / "Mixed"
    d.mkdir()
    for ext in ('jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'avif'):
        (d / f"page.{ext}").write_bytes(b"img")
    leafs = find_image_only_chapter_dirs(str(tmp_path))
    assert leafs == [str(d)]


# ───────────────────── pack_image_dir_to_cbz ─────────────────────


def test_packs_images_in_alphabetical_order(tmp_path):
    """Page order in the CBZ must match natural filename ordering."""
    from files import pack_image_dir_to_cbz

    src = tmp_path / "src"
    src.mkdir()
    for fname in ('003.jpg', '001.jpg', '002.jpg'):
        (src / fname).write_bytes(b"data-" + fname.encode())

    dst = tmp_path / "out.cbz"
    size = pack_image_dir_to_cbz(str(src), str(dst))
    assert size and size > 0
    assert dst.exists()

    with zipfile.ZipFile(dst) as zf:
        names = zf.namelist()
    assert names == ['001.jpg', '002.jpg', '003.jpg'], (
        f"expected sorted order; got {names}"
    )


def test_pack_excludes_metadata_files(tmp_path):
    from files import pack_image_dir_to_cbz
    src = tmp_path / "src"
    src.mkdir()
    (src / "001.jpg").write_bytes(b"img")
    (src / "Thumbs.db").write_bytes(b"junk")
    (src / ".DS_Store").write_bytes(b"junk")

    dst = tmp_path / "out.cbz"
    pack_image_dir_to_cbz(str(src), str(dst))
    with zipfile.ZipFile(dst) as zf:
        names = zf.namelist()
    assert names == ['001.jpg']


def test_pack_creates_parent_dir(tmp_path):
    """The parent of dst_cbz might not exist yet (staging is created
    on demand)."""
    from files import pack_image_dir_to_cbz
    src = tmp_path / "src"
    src.mkdir()
    (src / "001.jpg").write_bytes(b"img")

    dst = tmp_path / "deep" / "nested" / "out.cbz"
    assert not dst.parent.exists()
    pack_image_dir_to_cbz(str(src), str(dst))
    assert dst.exists()


def test_pack_returns_none_on_empty_dir(tmp_path):
    """No image files → no CBZ created → return None."""
    from files import pack_image_dir_to_cbz
    src = tmp_path / "src"
    src.mkdir()
    (src / "Thumbs.db").write_bytes(b"junk")  # only junk, no images

    dst = tmp_path / "out.cbz"
    result = pack_image_dir_to_cbz(str(src), str(dst))
    assert result is None
    assert not dst.exists()


def test_pack_is_uncompressed_for_speed(tmp_path):
    """Images are already JPEG/PNG-compressed; CBZ uses ZIP_STORED to
    avoid wasted CPU. Verify by checking compress_type."""
    from files import pack_image_dir_to_cbz
    src = tmp_path / "src"
    src.mkdir()
    (src / "001.jpg").write_bytes(b"jpg-data" * 1000)

    dst = tmp_path / "out.cbz"
    pack_image_dir_to_cbz(str(src), str(dst))
    with zipfile.ZipFile(dst) as zf:
        info = zf.getinfo('001.jpg')
        assert info.compress_type == zipfile.ZIP_STORED, (
            f"expected ZIP_STORED (no compression); got {info.compress_type}"
        )
