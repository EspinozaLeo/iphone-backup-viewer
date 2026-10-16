"""Camera roll: CameraRollDomain / Media/DCIM/."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import PurePosixPath

from ..core.backup import Backup
from ..core.models import FileEntry, FileKind, MediaItem, MediaKind

PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".tif", ".tiff", ".dng"})
VIDEO_SUFFIXES = frozenset({".mov", ".mp4", ".m4v", ".3gp"})

_DCIM = "Media/DCIM/"
# CameraRollDomain wins when both list the same path; MediaDomain/Media/DCIM shows up in some very
# old backups.
_DOMAINS = ("CameraRollDomain", "MediaDomain")
_LIVE_SUFFIX = ".mov"  # Live Photo motion is always QuickTime
_UNKNOWN_TIME = datetime.min.replace(tzinfo=timezone.utc)


def camera_roll(backup: Backup) -> list[MediaItem]:
    """Photos and videos in Media/DCIM, oldest first.

    A video with the same stem as a photo in the same folder (IMG_0001.HEIC + IMG_0001.MOV) is the
    Live Photo motion: it becomes that photo's `live_video`, not its own item. Sidecars (.AAE) are skipped.
    """
    # (folder, stem), case-insensitively -> the media files sharing it
    groups: dict[tuple[str, str], list[FileEntry]] = defaultdict(list)
    for entry in _dcim_media(backup):
        path = PurePosixPath(entry.relative_path.casefold())
        groups[(str(path.parent), path.stem)].append(entry)

    items: list[MediaItem] = []
    for group in groups.values():
        group.sort(key=lambda e: e.relative_path)
        photos = [e for e in group if e.suffix in PHOTO_SUFFIXES]
        videos = [e for e in group if e.suffix in VIDEO_SUFFIXES]
        live = next((v for v in videos if v.suffix == _LIVE_SUFFIX), None) if photos else None
        for index, photo in enumerate(photos):
            items.append(MediaItem(photo, MediaKind.PHOTO, photo.modified, live if index == 0 else None))
        for video in videos:
            if video is not live:
                items.append(MediaItem(video, MediaKind.VIDEO, video.modified))

    items.sort(key=_oldest_first)
    return items


def _dcim_media(backup: Backup) -> list[FileEntry]:
    """Regular photo/video files under Media/DCIM/, de-duplicated across domains.

    Data files are not checked: an entry whose file is missing is kept so the UI can show a placeholder.
    """
    seen: set[str] = set()
    found: list[FileEntry] = []
    for domain in _DOMAINS:
        for entry in backup.under(domain, _DCIM):
            if entry.kind is not FileKind.FILE or entry.name.startswith("."):
                continue
            if entry.suffix not in PHOTO_SUFFIXES and entry.suffix not in VIDEO_SUFFIXES:
                continue  # .AAE edit sidecars, .plist, .MISC folder contents, ...
            key = entry.relative_path.casefold()
            if key not in seen:
                seen.add(key)
                found.append(entry)
    return found


def _oldest_first(item: MediaItem) -> tuple:
    # Unknown capture times go last; path then domain make the order stable and deterministic.
    return (item.taken is None, item.taken or _UNKNOWN_TIME, item.entry.relative_path, item.entry.domain)
