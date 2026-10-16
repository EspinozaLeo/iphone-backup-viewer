from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, UnidentifiedImageError

from iphone_backup_viewer.core.backup import Backup
from iphone_backup_viewer.media.images import open_image
from tests.fixtures import fake_backup as fb


def _is_white(pixel) -> bool:
    return min(pixel[:3]) > 225


def _fixture_file(backup: Backup, path: str) -> Path:
    return backup.local_path(backup.find("CameraRollDomain", path))


def test_exif_orientation_6_is_turned_upright(modern_backup: Backup) -> None:
    path = _fixture_file(modern_backup, fb.ROTATED_PHOTO)
    with Image.open(path) as raw:
        assert raw.size == (480, 360)  # stored sideways
    image = open_image(path)
    assert image.size == (360, 480)
    assert image.mode == "RGB"
    assert _is_white(image.getpixel((180, 10)))  # the band is on top once upright
    assert not _is_white(image.getpixel((180, 470)))
    assert not _is_white(image.getpixel((10, 240)))


def test_plain_photo_and_downscale(modern_backup: Backup) -> None:
    path = _fixture_file(modern_backup, "Media/DCIM/100APPLE/IMG_0001.JPG")
    assert open_image(path).size == (480, 360)
    small = open_image(path, max_size=100)
    assert small.size == (100, 75)
    assert _is_white(small.getpixel((50, 2)))


def test_rotated_photo_downscale_keeps_orientation(modern_backup: Backup) -> None:
    small = open_image(_fixture_file(modern_backup, fb.ROTATED_PHOTO), max_size=64)
    assert small.size == (48, 64)


def test_large_jpeg_uses_draft_and_still_fits(tmp_path: Path) -> None:
    path = tmp_path / "big.jpg"
    Image.new("RGB", (4032, 3024), (200, 30, 30)).save(path, "JPEG")
    image = open_image(path, max_size=256)
    assert image.size == (256, 192)
    assert image.getpixel((128, 96))[0] > 150


def test_screenshot_png_keeps_alpha(modern_backup: Backup) -> None:
    image = open_image(_fixture_file(modern_backup, fb.SCREENSHOT))
    assert image.mode == "RGBA"
    assert image.size == (300, 650)


@pytest.mark.parametrize(
    ("make", "fmt", "expected_mode"),
    [
        (lambda: Image.new("L", (20, 10), 128), "JPEG", "RGB"),
        (lambda: Image.new("CMYK", (20, 10), (0, 255, 255, 0)), "JPEG", "RGB"),
        (lambda: Image.new("LA", (20, 10), (128, 100)), "PNG", "RGBA"),
        (lambda: Image.new("1", (20, 10), 1), "PNG", "RGB"),
        (lambda: Image.new("RGB", (20, 10), (1, 2, 3)), "GIF", "RGB"),
    ],
)
def test_modes_are_normalised(tmp_path: Path, make, fmt: str, expected_mode: str) -> None:
    path = tmp_path / f"img.{fmt.lower()}"
    make().save(path, fmt)
    assert open_image(path).mode == expected_mode


def test_palette_with_transparency_becomes_rgba(tmp_path: Path) -> None:
    image = Image.new("P", (10, 10), 0)
    image.putpalette([255, 0, 0] * 256)
    path = tmp_path / "t.png"
    image.save(path, transparency=0)
    assert open_image(path).mode == "RGBA"


def test_bad_exif_does_not_lose_the_picture(tmp_path: Path) -> None:
    path = tmp_path / "bad-exif.jpg"
    exif = Image.Exif()
    exif[0x0112] = 99  # not a valid orientation
    Image.new("RGB", (40, 30), (0, 0, 255)).save(path, "JPEG", exif=exif.tobytes())
    assert open_image(path).size == (40, 30)


def test_undecodable_file_raises(tmp_path: Path, modern_backup: Backup) -> None:
    junk = tmp_path / "junk"
    junk.write_bytes(b"definitely not an image")
    with pytest.raises(UnidentifiedImageError):
        open_image(junk)
    with pytest.raises(OSError):
        open_image(_fixture_file(modern_backup, fb.CORRUPT_PHOTO))


def test_heic_opener_is_registered(tmp_path: Path) -> None:
    path = tmp_path / "x.png"
    Image.new("RGB", (2, 2)).save(path)
    open_image(path)
    assert Image.registered_extensions().get(".heic") == "HEIF"


def test_accepts_file_without_extension(modern_backup: Backup) -> None:
    path = _fixture_file(modern_backup, "Media/DCIM/100APPLE/IMG_0002.JPG")
    assert path.suffix == ""
    assert open_image(path).size == (480, 360)
