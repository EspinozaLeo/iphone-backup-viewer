from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from pathlib import Path

import platformdirs
from PIL import Image

from ..core.backup import Backup
from ..core.errors import BackupError, EncryptedBackupError
from ..core.models import FileEntry
from .images import open_image
from .video import VIDEO_SUFFIXES, video_frame

log = logging.getLogger(__name__)

APP_CACHE_NAME = "iPhoneBackupViewer"
_JPEG_QUALITY = 85
_BACKGROUND = (255, 255, 255)  # JPEG has no alpha; transparent PNGs are flattened onto white
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def default_cache_dir() -> Path:
    """Per-user cache (platformdirs), e.g. %LOCALAPPDATA%\\iPhoneBackupViewer\\Cache on Windows."""
    return Path(platformdirs.user_cache_dir(APP_CACHE_NAME, appauthor=False))


class ThumbnailCache:
    """JPEG thumbnails on disk, keyed by backup UDID + file_id + size. Thread-safe."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self._lock = threading.Lock()
        # (backup folder, file_id) of media that failed to decode or has no data file. In memory
        # only: one attempt per file per session, so a broken file is not retried on every scroll.
        self._failed: set[tuple[str, str]] = set()

    def get(self, backup: Backup, entry: FileEntry, size: int = 256) -> Path | None:
        """Path to a cached thumbnail (longest side `size`), generating it if needed.

        Photos via media.images; videos from a frame about 1 s in (PyAV). None if the file is
        missing or undecodable. Never raises for bad media, because one corrupt file must not break a grid.
        """
        if size < 1:
            raise ValueError(f"Thumbnail size must be positive, got {size}")
        try:
            return self._get(backup, entry, size)
        except Exception:  # a bug here must still not take the grid down; log it loudly instead
            log.exception("Unexpected error making a thumbnail for %s/%s", entry.domain, entry.relative_path)
            return None

    def _get(self, backup: Backup, entry: FileEntry, size: int) -> Path | None:
        failed_key = (str(backup.info.path), entry.file_id)
        with self._lock:
            if failed_key in self._failed:
                return None

        target = self.cache_dir / _safe(backup.info.udid) / "thumbs" / f"{_safe(entry.file_id)}_{size}.jpg"
        if target.is_file():
            return target

        try:
            source = backup.local_path(entry)
        except EncryptedBackupError:
            return None  # not a property of the file: it may unlock later, so do not remember it
        except BackupError as exc:  # MissingFileError, invalid file ID
            log.info("No thumbnail for %s/%s: %s", entry.domain, entry.relative_path, exc)
            self._remember_failure(failed_key)
            return None

        try:
            image = self._render(source, entry, size)
        except Exception as exc:  # Pillow, pi-heif and PyAV each raise their own types for bad data
            log.warning("Cannot decode %s/%s (%s): %s", entry.domain, entry.relative_path, source.name, exc)
            self._remember_failure(failed_key)
            return None

        try:
            self._write(image, target)
        except OSError as exc:  # cache trouble (disk full, permissions) is transient: do not remember
            log.warning("Cannot write thumbnail %s: %s", target, exc)
            return None
        return target

    def _remember_failure(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._failed.add(key)

    @staticmethod
    def _render(source: Path, entry: FileEntry, size: int) -> Image.Image:
        if entry.suffix in VIDEO_SUFFIXES:
            image = video_frame(source, size)
        else:
            image = open_image(source, max_size=size)
        if image.mode == "RGBA":
            flat = Image.new("RGB", image.size, _BACKGROUND)
            flat.paste(image, mask=image.getchannel("A"))
            image = flat
        elif image.mode != "RGB":
            image = image.convert("RGB")
        return image

    @staticmethod
    def _write(image: Image.Image, target: Path) -> None:
        """Write to a unique temp name, then rename: readers never see a half-written JPEG."""
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.tmp")
        try:
            image.save(temp, "JPEG", quality=_JPEG_QUALITY)
            try:
                os.replace(temp, target)
            except PermissionError:
                # Windows refuses to replace a file another thread or the UI has open. That file is
                # a finished thumbnail of the same media, so it is as good as ours.
                if not target.is_file():
                    raise
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def _safe(name: str) -> str:
    """Keep plist- or manifest-supplied names from escaping the cache folder."""
    cleaned = _UNSAFE.sub("_", name).strip(".")
    return cleaned or "_"
