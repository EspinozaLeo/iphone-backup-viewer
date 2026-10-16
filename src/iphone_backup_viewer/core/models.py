"""Shared value types. This file is the contract between core/, extractors/, media/ and ui/."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath


class ManifestFormat(str, Enum):
    MBDB = "mbdb"  # iOS 4–9: binary Manifest.mbdb, flat file layout
    SQLITE = "sqlite"  # iOS 10+: Manifest.db, files under fileID[:2]/fileID


class FileKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """One backup folder, summarised from its plists. Building one never opens the manifest."""

    path: Path
    udid: str
    device_name: str
    product_type: str | None  # e.g. "iPhone10,3"
    ios_version: str | None  # e.g. "16.4.1"
    last_backup: datetime | None  # timezone-aware, UTC
    is_encrypted: bool
    manifest_format: ManifestFormat | None  # None: no manifest found (incomplete or in-progress backup)


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One record from the manifest: a file as it existed on the phone."""

    file_id: str  # SHA1 hex of f"{domain}-{relative_path}"
    domain: str  # e.g. "CameraRollDomain"
    relative_path: str  # e.g. "Media/DCIM/100APPLE/IMG_0001.JPG"
    kind: FileKind
    size: int
    modified: datetime | None  # timezone-aware, UTC
    protection_class: int | None = None
    wrapped_key: bytes | None = None  # per-file key, still wrapped; used once encryption lands

    @property
    def name(self) -> str:
        return PurePosixPath(self.relative_path).name

    @property
    def suffix(self) -> str:
        """Lower-case extension including the dot, e.g. ".heic"."""
        return PurePosixPath(self.relative_path).suffix.lower()


class MediaKind(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class MediaItem:
    """One camera-roll item. A Live Photo is a single item: the still plus its paired video."""

    entry: FileEntry
    kind: MediaKind
    taken: datetime | None  # best known capture time, UTC
    live_video: FileEntry | None = None
