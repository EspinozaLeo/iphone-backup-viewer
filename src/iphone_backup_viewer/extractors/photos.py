"""Camera roll: CameraRollDomain / Media/DCIM/."""

from __future__ import annotations

from ..core.backup import Backup
from ..core.models import MediaItem

PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".tif", ".tiff", ".dng"})
VIDEO_SUFFIXES = frozenset({".mov", ".mp4", ".m4v", ".3gp"})


def camera_roll(backup: Backup) -> list[MediaItem]:
    """Photos and videos in Media/DCIM, oldest first.

    A video with the same stem as a photo in the same folder (IMG_0001.HEIC + IMG_0001.MOV) is the
    Live Photo motion: it becomes that photo's `live_video`, not its own item. Sidecars (.AAE) are skipped.
    """
    raise NotImplementedError
