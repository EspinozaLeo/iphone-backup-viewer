"""Manifest readers, one per on-disk format. Each returns every record as a FileEntry."""

from __future__ import annotations

from pathlib import Path

from ..errors import BackupError
from ..models import FileEntry, ManifestFormat
from .mbdb import read_mbdb
from .sqlite import read_manifest_db

MANIFEST_FILES = {
    ManifestFormat.SQLITE: "Manifest.db",
    ManifestFormat.MBDB: "Manifest.mbdb",
}


def read_manifest(backup_dir: Path, manifest_format: ManifestFormat | None) -> list[FileEntry]:
    """Every record of the backup's manifest. Raises BackupError if it is missing or unreadable."""
    if manifest_format is ManifestFormat.SQLITE:
        return read_manifest_db(backup_dir / MANIFEST_FILES[manifest_format])
    if manifest_format is ManifestFormat.MBDB:
        return read_mbdb(backup_dir / MANIFEST_FILES[manifest_format])
    raise BackupError(f"No Manifest.db or Manifest.mbdb in {backup_dir} (incomplete or in-progress backup?)")


__all__ = ["MANIFEST_FILES", "read_manifest", "read_manifest_db", "read_mbdb"]
