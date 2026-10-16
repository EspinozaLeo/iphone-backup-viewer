"""Finding backups on disk and reading their summary plists."""

from __future__ import annotations

import logging
import os
import plistlib
import sys
from datetime import datetime
from pathlib import Path

from ._time import as_utc
from .errors import BackupError
from .models import BackupInfo, ManifestFormat

log = logging.getLogger(__name__)

# Any one of these makes a folder a backup. Status.plist alone is an interrupted first backup:
# it is still listed (manifest_format None) so the user sees why it will not open.
_BACKUP_MARKERS = ("Info.plist", "Manifest.plist", "Manifest.db", "Manifest.mbdb", "Status.plist")
_SNAPSHOT_DIR = "snapshot"
_UNKNOWN_DEVICE = "Unknown device"


def default_backup_roots() -> list[Path]:
    """Folders where iTunes / the Apple Devices app keep backups on this OS, existing ones only.

    Windows: %APPDATA%\\Apple Computer\\MobileSync\\Backup and %USERPROFILE%\\Apple\\MobileSync\\Backup.
    macOS: ~/Library/Application Support/MobileSync/Backup.
    """
    candidates: list[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Apple Computer" / "MobileSync" / "Backup")
        profile = os.environ.get("USERPROFILE")
        candidates.append((Path(profile) if profile else Path.home()) / "Apple" / "MobileSync" / "Backup")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup")

    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
            key = os.path.normcase(str(candidate.resolve()))
        except OSError:
            continue
        if key not in seen:  # both env vars can point at the same folder via a junction
            seen.add(key)
            roots.append(candidate)
    return roots


def read_info(path: Path) -> BackupInfo:
    """Summarise one backup folder from Info.plist, Manifest.plist and Status.plist.

    Tolerates any one plist being missing. Raises BackupError if the folder is not a backup at all.
    """
    path = Path(path)
    if not path.is_dir():
        raise BackupError(f"Not a folder: {path}")
    if not _is_backup_dir(path):
        raise BackupError(f"Not an iPhone backup (no Info.plist, Manifest.* or Status.plist): {path}")

    info = _load_plist(path / "Info.plist")
    manifest = _load_plist(path / "Manifest.plist")
    status = _load_plist(path / "Status.plist")
    lockdown = manifest.get("Lockdown")
    if not isinstance(lockdown, dict):
        lockdown = {}

    if (path / "Manifest.db").is_file():
        manifest_format: ManifestFormat | None = ManifestFormat.SQLITE
    elif (path / "Manifest.mbdb").is_file():
        manifest_format = ManifestFormat.MBDB
    else:
        manifest_format = None

    return BackupInfo(
        path=path,
        udid=_first_str(
            info.get("Unique Identifier"),
            info.get("Target Identifier"),
            lockdown.get("UniqueDeviceID"),
        )
        or path.name,
        device_name=_first_str(info.get("Device Name"), info.get("Display Name"), lockdown.get("DeviceName"))
        or _UNKNOWN_DEVICE,
        product_type=_first_str(info.get("Product Type"), lockdown.get("ProductType")),
        ios_version=_first_str(info.get("Product Version"), lockdown.get("ProductVersion")),
        last_backup=_first_date(info.get("Last Backup Date"), manifest.get("Date"), status.get("Date")),
        is_encrypted=_flag(manifest.get("IsEncrypted")),
        manifest_format=manifest_format,
    )


def scan(root: Path) -> list[BackupInfo]:
    """Backups in `root`: either `root` itself is a backup, or its direct children are.

    Skips folders that are not backups (and in-progress Snapshot dirs) instead of raising.
    Newest `last_backup` first.
    """
    root = Path(root)
    try:
        if not root.is_dir():
            return []
        if _is_backup_dir(root):
            return [read_info(root)]
        children = sorted(root.iterdir())
    except (BackupError, OSError) as exc:
        log.warning("Cannot scan %s: %s", root, exc)
        return []

    found: list[BackupInfo] = []
    for child in children:
        if child.name.casefold() == _SNAPSHOT_DIR:
            continue
        try:
            if child.is_dir() and _is_backup_dir(child):
                found.append(read_info(child))
        except (BackupError, OSError) as exc:
            log.info("Skipping %s: %s", child, exc)
    found.sort(key=_newest_first)
    return found


def _newest_first(info: BackupInfo) -> tuple:
    if info.last_backup is None:
        return (1, 0.0, info.path.name.casefold())
    return (0, -info.last_backup.timestamp(), info.path.name.casefold())


def _is_backup_dir(path: Path) -> bool:
    return any((path / name).is_file() for name in _BACKUP_MARKERS)


def _load_plist(path: Path) -> dict:
    """The plist as a dict, or {} if it is missing or unreadable. plistlib reads XML and binary."""
    try:
        with path.open("rb") as fh:
            value = plistlib.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # OSError, InvalidFileException, ExpatError, ValueError, ...
        log.warning("Ignoring unreadable %s: %s", path, exc)
        return {}
    return value if isinstance(value, dict) else {}


def _flag(value: object) -> bool:
    """A plist boolean; tolerate the integer form some tools write. A string is never truthy here."""
    return value is True or (isinstance(value, int) and not isinstance(value, bool) and value != 0)


def _first_str(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_date(*values: object) -> datetime | None:
    for value in values:
        if isinstance(value, datetime):
            return as_utc(value)
        if isinstance(value, str) and value.strip():
            try:
                return as_utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
            except ValueError:
                continue
    return None
