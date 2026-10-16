"""Finding backups on disk and reading their summary plists."""

from __future__ import annotations

from pathlib import Path

from .models import BackupInfo


def default_backup_roots() -> list[Path]:
    """Folders where iTunes / the Apple Devices app keep backups on this OS, existing ones only.

    Windows: %APPDATA%\\Apple Computer\\MobileSync\\Backup and %USERPROFILE%\\Apple\\MobileSync\\Backup.
    macOS: ~/Library/Application Support/MobileSync/Backup.
    """
    raise NotImplementedError


def read_info(path: Path) -> BackupInfo:
    """Summarise one backup folder from Info.plist, Manifest.plist and Status.plist.

    Tolerates any one plist being missing. Raises BackupError if the folder is not a backup at all.
    """
    raise NotImplementedError


def scan(root: Path) -> list[BackupInfo]:
    """Backups in `root`: either `root` itself is a backup, or its direct children are.

    Skips folders that are not backups (and in-progress Snapshot dirs) instead of raising.
    Newest `last_backup` first.
    """
    raise NotImplementedError
