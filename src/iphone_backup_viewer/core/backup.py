"""An opened backup: its manifest, and access to the files it contains. Read-only, always."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from .models import BackupInfo, FileEntry


class Backup:
    """Never writes inside the backup folder. Safe to share across threads once opened."""

    def __init__(self, info: BackupInfo) -> None:
        self.info = info

    @classmethod
    def open(cls, path: Path) -> Backup:
        """Read the plists and the manifest (Manifest.mbdb or Manifest.db). Raises BackupError."""
        raise NotImplementedError

    def entries(self) -> list[FileEntry]:
        """Every manifest record. Loaded once and cached."""
        raise NotImplementedError

    def find(self, domain: str, relative_path: str) -> FileEntry | None:
        """Exact lookup, e.g. find("HomeDomain", "Library/SMS/sms.db")."""
        raise NotImplementedError

    def under(self, domain: str, path_prefix: str = "") -> Iterable[FileEntry]:
        """Entries in `domain` whose relative_path starts with `path_prefix`."""
        raise NotImplementedError

    def local_path(self, entry: FileEntry) -> Path:
        """A plaintext copy of the file on disk that callers may read but must not modify.

        Unencrypted backups return the backup's own data file. The file usually has no
        extension. Raises MissingFileError, or EncryptedBackupError until unlocking is supported.
        """
        raise NotImplementedError

    def open_file(self, entry: FileEntry) -> BinaryIO:
        raise NotImplementedError

    def read_bytes(self, entry: FileEntry) -> bytes:
        raise NotImplementedError
