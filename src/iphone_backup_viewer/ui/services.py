"""Everything the UI calls below it, gathered in one place so tests (and the app, before core/
lands) can swap in fakes. The UI never reaches into core/, extractors/ or media/ any other way."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from ..core import discovery
from ..core.backup import Backup
from ..core.models import BackupInfo, FileEntry, MediaItem
from ..extractors import photos
from ..media import images, thumbnails


class ThumbnailSource(Protocol):
    """What the grid needs from media.thumbnails.ThumbnailCache."""

    cache_dir: Path

    def get(self, backup: Any, entry: FileEntry, size: int = 256) -> Path | None: ...


@dataclass
class Services:
    """Production defaults; pass replacements as keyword arguments."""

    default_roots: Callable[[], list[Path]] = discovery.default_backup_roots
    scan: Callable[[Path], list[BackupInfo]] = discovery.scan
    open_backup: Callable[[Path], Backup] = Backup.open
    camera_roll: Callable[[Backup], list[MediaItem]] = photos.camera_roll
    open_image: Callable[..., Image.Image] = images.open_image
    thumbnails: ThumbnailSource | None = None  # None: ThumbnailCache(default_cache_dir()) on first use

    def thumbnail_cache(self) -> ThumbnailSource:
        if self.thumbnails is None:
            self.thumbnails = thumbnails.ThumbnailCache(thumbnails.default_cache_dir())
        return self.thumbnails
