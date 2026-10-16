from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from iphone_backup_viewer.core.backup import Backup
from iphone_backup_viewer.core.discovery import read_info
from iphone_backup_viewer.media import thumbnails, video
from iphone_backup_viewer.media.thumbnails import ThumbnailCache, default_cache_dir
from tests.fixtures import fake_backup as fb


@pytest.fixture
def cache(tmp_path: Path) -> ThumbnailCache:
    return ThumbnailCache(tmp_path / "cache")


def _entry(backup: Backup, path: str):
    entry = backup.find("CameraRollDomain", path)
    assert entry is not None
    return entry


def _brightness(pixel) -> int:
    return sum(pixel[:3]) // 3


def test_default_cache_dir() -> None:
    path = default_cache_dir()
    assert isinstance(path, Path)
    assert "iPhoneBackupViewer" in path.parts


def test_photo_thumbnail_path_and_size(cache: ThumbnailCache, modern_backup: Backup) -> None:
    entry = _entry(modern_backup, "Media/DCIM/100APPLE/IMG_0001.JPG")
    path = cache.get(modern_backup, entry)
    assert path == cache.cache_dir / fb.MODERN_UDID / "thumbs" / f"{entry.file_id}_256.jpg"
    with Image.open(path) as image:
        assert image.format == "JPEG"
        assert image.size == (256, 192)


def test_rotated_photo_thumbnail_is_upright(cache: ThumbnailCache, legacy_backup: Backup) -> None:
    with Image.open(cache.get(legacy_backup, _entry(legacy_backup, fb.ROTATED_PHOTO))) as image:
        assert image.size == (192, 256)
        assert _brightness(image.getpixel((96, 8))) > 220


def test_screenshot_is_flattened_to_rgb(cache: ThumbnailCache, modern_backup: Backup) -> None:
    with Image.open(cache.get(modern_backup, _entry(modern_backup, fb.SCREENSHOT))) as image:
        assert image.mode == "RGB"
        assert max(image.size) == 256


@pytest.mark.parametrize("which", ["modern", "legacy"])
def test_portrait_video_thumbnail(cache: ThumbnailCache, modern_backup, legacy_backup, which) -> None:
    backup = modern_backup if which == "modern" else legacy_backup
    path = cache.get(backup, _entry(backup, fb.PORTRAIT_VIDEO))
    assert path is not None
    with Image.open(path) as image:
        assert image.size == (144, 256)  # display matrix applied: portrait, not sideways
        assert _brightness(image.getpixel((72, 8))) > 200  # white band on top
        r, g, b = image.getpixel((72, 200))
        assert g > b  # green frames start at 1 s; blue would mean we kept the first frame


def test_landscape_video_thumbnail(cache: ThumbnailCache, modern_backup: Backup) -> None:
    with Image.open(cache.get(modern_backup, _entry(modern_backup, fb.LIVE_VIDEO))) as image:
        assert image.size == (256, 192)


def test_sizes_are_cached_separately(cache: ThumbnailCache, modern_backup: Backup) -> None:
    entry = _entry(modern_backup, "Media/DCIM/100APPLE/IMG_0002.JPG")
    small, large = cache.get(modern_backup, entry, 96), cache.get(modern_backup, entry, 400)
    assert small != large
    with Image.open(small) as a, Image.open(large) as b:
        assert max(a.size) == 96 and max(b.size) == 400


def test_corrupt_file_returns_none_and_is_not_retried(
    cache: ThumbnailCache, modern_backup: Backup, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(modern_backup, fb.CORRUPT_PHOTO)
    calls = []
    real = thumbnails.open_image

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(thumbnails, "open_image", counting)
    assert cache.get(modern_backup, entry) is None
    assert cache.get(modern_backup, entry) is None
    assert cache.get(modern_backup, entry, 128) is None
    assert len(calls) == 1
    assert not (cache.cache_dir / fb.MODERN_UDID / "thumbs" / f"{entry.file_id}_256.jpg").exists()


def test_corrupt_video_returns_none(cache: ThumbnailCache, tmp_path: Path, modern_path: Path) -> None:
    import shutil

    copy = tmp_path / "copy"
    shutil.copytree(modern_path, copy)
    backup = Backup.open(copy)
    entry = _entry(backup, fb.PORTRAIT_VIDEO)
    data = backup.local_path(entry)
    data.write_bytes(data.read_bytes()[:300])  # cut off mid-header
    assert cache.get(backup, entry) is None


def test_missing_file_returns_none(cache: ThumbnailCache, modern_backup: Backup) -> None:
    assert cache.get(modern_backup, _entry(modern_backup, fb.MISSING_PHOTO)) is None


def test_encrypted_backup_returns_none(cache: ThumbnailCache, modern_backup: Backup, modern_path: Path) -> None:
    locked = Backup(replace(read_info(modern_path), is_encrypted=True))
    entry = _entry(modern_backup, "Media/DCIM/100APPLE/IMG_0001.JPG")
    assert cache.get(locked, entry) is None


def test_cache_is_reused(cache: ThumbnailCache, modern_backup: Backup, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _entry(modern_backup, "Media/DCIM/100APPLE/IMG_0003.JPG")
    first = cache.get(modern_backup, entry)
    stamp = first.stat().st_mtime_ns

    def boom(*args, **kwargs):
        raise AssertionError("should have used the cached thumbnail")

    monkeypatch.setattr(ThumbnailCache, "_render", staticmethod(boom))
    assert cache.get(modern_backup, entry) == first
    assert ThumbnailCache(cache.cache_dir).get(modern_backup, entry) == first  # survives a restart
    assert first.stat().st_mtime_ns == stamp


def test_concurrent_requests_for_one_thumbnail(cache: ThumbnailCache, modern_backup: Backup) -> None:
    entry = _entry(modern_backup, "Media/DCIM/101APPLE/IMG_0005.JPG")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: cache.get(modern_backup, entry), range(16)))
    assert len(set(results)) == 1 and results[0] is not None
    with Image.open(results[0]) as image:
        image.load()
    leftovers = [p for p in results[0].parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_whole_grid_in_parallel(cache: ThumbnailCache, modern_backup: Backup) -> None:
    from iphone_backup_viewer.extractors.photos import camera_roll

    items = camera_roll(modern_backup)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = dict(zip((i.entry.relative_path for i in items), pool.map(lambda i: cache.get(modern_backup, i.entry), items)))
    assert results[fb.MISSING_PHOTO] is None and results[fb.CORRUPT_PHOTO] is None
    assert sum(1 for p in results.values() if p is not None) == len(items) - 2


def test_invalid_size_is_a_programming_error(cache: ThumbnailCache, modern_backup: Backup) -> None:
    with pytest.raises(ValueError):
        cache.get(modern_backup, _entry(modern_backup, "Media/DCIM/100APPLE/IMG_0001.JPG"), 0)


def test_video_seek_failure_falls_back_to_first_frame(modern_backup: Backup, monkeypatch: pytest.MonkeyPatch) -> None:
    path = modern_backup.local_path(_entry(modern_backup, fb.PORTRAIT_VIDEO))
    real = video._frame
    seen = []

    def flaky(p, seconds, max_size):
        seen.append(seconds)
        if seconds > 0:
            raise RuntimeError("seek exploded")
        return real(p, seconds, max_size)

    monkeypatch.setattr(video, "_frame", flaky)
    image = video.video_frame(path, 128)
    assert seen == [1.0, 0.0]
    r, g, b = image.getpixel((image.width // 2, image.height - 20))
    assert b > g  # first frame is the blue one


def test_short_video_seeks_to_its_middle(tmp_path: Path) -> None:
    path = tmp_path / "short.mov"
    path.write_bytes(fb.mov_bytes("short", seconds=0.6, fps=10))
    image = video.video_frame(path, 64)
    assert image.size == (64, 36)


def test_no_qt_below_the_ui_layer() -> None:
    code = (
        "import sys\n"
        "import iphone_backup_viewer.core.discovery, iphone_backup_viewer.core.backup\n"
        "import iphone_backup_viewer.extractors.photos, iphone_backup_viewer.media.thumbnails\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('PySide6', 'shiboken6', 'PyQt5', 'PyQt6'))\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    src = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run([sys.executable, "-c", code], cwd=src, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
