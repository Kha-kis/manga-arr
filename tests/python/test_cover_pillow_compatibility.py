"""Benign format and pixel compatibility at the real cover image boundary."""

import asyncio
from io import BytesIO
from pathlib import Path
import zipfile

import httpx
from PIL import Image
import pytest

import cover_images


def _encode(image: Image.Image, image_format: str) -> bytes:
    output = BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_cbz_supported_formats_decode_to_rgb_jpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image_format: str
) -> None:
    monkeypatch.setattr(cover_images, "COVERS_DIR", str(tmp_path / "covers"))
    payload = _encode(Image.new("RGB", (36, 54), (30, 90, 150)), image_format)
    archive = tmp_path / "volume.cbz"
    with zipfile.ZipFile(archive, "w") as cbz:
        cbz.writestr(f"001.{image_format.lower()}", payload)

    assert cover_images.extract_cbz_cover(7, str(archive)) is True
    dest = tmp_path / "covers/7.jpg"
    assert cover_images.cached_cover_is_valid(str(dest)) is True
    with Image.open(dest) as image:
        image.load()
        assert (image.format, image.mode, image.size) == ("JPEG", "RGB", (36, 54))
        assert image.getpixel((18, 27)) == pytest.approx((30, 90, 150), abs=4)


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_transparency_is_flattened_on_white(mode: str) -> None:
    if mode == "P":
        source = Image.new("P", (32, 48), 0)
        source.putpalette([0, 0, 0, 20, 40, 60] + [0] * 762)
        source.info["transparency"] = 0
        source.paste(1, (16, 0, 32, 48))
        expected = (20, 40, 60)
    else:
        source = Image.new(mode, (32, 48), (0, 0, 0, 0) if mode == "RGBA" else (0, 0))
        source.paste(
            (20, 40, 60, 128) if mode == "RGBA" else (40, 128), (16, 0, 32, 48)
        )
        expected = (137, 147, 157) if mode == "RGBA" else (147, 147, 147)

    data, image_format = cover_images._normalize_cover(_encode(source, "PNG"))
    assert image_format == "png"
    with Image.open(BytesIO(data)) as image:
        image.load()
        assert image.mode == "RGB"
        assert image.getpixel((4, 24)) == pytest.approx((255, 255, 255), abs=3)
        assert image.getpixel((28, 24)) == pytest.approx(expected, abs=4)


def test_exif_orientation_is_applied_before_jpeg_encoding() -> None:
    source = Image.new("RGB", (40, 20), "red")
    source.paste("blue", (20, 0, 40, 20))
    exif = Image.Exif()
    exif[274] = 6
    output = BytesIO()
    source.save(output, format="JPEG", exif=exif)

    data, image_format = cover_images._normalize_cover(output.getvalue())
    assert image_format == "jpeg"
    with Image.open(BytesIO(data)) as image:
        image.load()
        assert image.size == (20, 40)
        assert image.getexif().get(274) is None
        assert image.getpixel((10, 5)) == pytest.approx((255, 0, 0), abs=5)
        assert image.getpixel((10, 35)) == pytest.approx((0, 0, 255), abs=5)


@pytest.mark.parametrize("mode", ["RGBA", "LA"])
def test_alpha_thumbnail_preserves_aspect_and_compositing(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(cover_images, "MAX_COVER_SIZE", (24, 36))
    source = Image.new(
        mode, (320, 640), (20, 40, 60, 128) if mode == "RGBA" else (40, 128)
    )
    data, _ = cover_images._normalize_cover(_encode(source, "PNG"))
    expected = (137, 147, 157) if mode == "RGBA" else (147, 147, 147)
    with Image.open(BytesIO(data)) as image:
        image.load()
        assert image.size == (18, 36)
        assert image.getpixel((9, 18)) == pytest.approx(expected, abs=4)


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_forced_refresh_and_cached_read_use_normalized_cover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image_format: str
) -> None:
    monkeypatch.setattr(cover_images, "COVERS_DIR", str(tmp_path))
    monkeypatch.setattr("security.validate_outbound_url", lambda _url: None)
    monkeypatch.setattr(cover_images, "log_event", lambda *_args: None)
    payload = _encode(Image.new("RGB", (36, 54), "green"), image_format)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(cover_images.httpx, "AsyncClient", lambda **_kwargs: client)
    url = "https://cdn.example.test/cover"
    result = asyncio.run(cover_images.download_cover(7, url, force=True))
    assert result["ok"] is True
    assert result["source_format"] == image_format.lower()
    dest = tmp_path / "7.jpg"
    normalized = dest.read_bytes()
    assert cover_images.cached_cover_is_valid(str(dest)) is True

    cached = asyncio.run(cover_images.download_cover(7, url, force=False))
    assert cached["status"] == "cached"
    assert len(requests) == 1
    assert dest.read_bytes() == normalized


@pytest.mark.parametrize("payload", [b"", b"not an image"])
def test_invalid_cbz_image_does_not_create_cover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    monkeypatch.setattr(cover_images, "COVERS_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(cover_images, "log_event", lambda *_args: None)
    archive = tmp_path / "invalid.cbz"
    with zipfile.ZipFile(archive, "w") as cbz:
        cbz.writestr("001.png", payload)
    assert cover_images.extract_cbz_cover(7, str(archive)) is False
    assert not (tmp_path / "covers/7.jpg").exists()
