from __future__ import annotations

from pathlib import Path

from ..core.backup import Backup
from ..core.models import FileEntry


def default_cache_dir() -> Path:
    """Per-user cache (platformdirs), e.g. %LOCALAPPDATA%\\iPhoneBackupViewer\\Cache on Windows."""
    raise NotImplementedError


class ThumbnailCache:
    """JPEG thumbnails on disk, keyed by backup UDID + file_id + size. Thread-safe."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    def get(self, backup: Backup, entry: FileEntry, size: int = 256) -> Path | None:
        """Path to a cached thumbnail (longest side `size`), generating it if needed.

        Photos via media.images; videos from a frame about 1 s in (PyAV). None if the file is
        missing or undecodable. Never raises for bad media, because one corrupt file must not break a grid.
        """
        raise NotImplementedError
