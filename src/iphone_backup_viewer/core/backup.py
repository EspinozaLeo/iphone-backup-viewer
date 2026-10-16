"""An opened backup: its manifest, and access to the files it contains. Read-only, always."""

from __future__ import annotations

import re
import threading
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from .discovery import read_info
from .errors import BackupError, EncryptedBackupError, MissingFileError
from .manifest import read_manifest
from .models import BackupInfo, FileEntry, ManifestFormat

# A file ID is a SHA1 hex digest. Anything else in a manifest is refused rather than joined onto
# the backup path, so a crafted fileID like "../../x" can never point outside the backup folder.
_FILE_ID = re.compile(r"[0-9a-f]{40}")


class Backup:
    """Never writes inside the backup folder. Safe to share across threads once opened."""

    def __init__(self, info: BackupInfo) -> None:
        self.info = info
        self._load_lock = threading.Lock()
        self._loaded = False
        self._entries: tuple[FileEntry, ...] = ()
        self._by_key: dict[tuple[str, str], FileEntry] = {}
        # domain -> (relative paths sorted, entries in the same order); bisect finds a prefix range
        self._by_domain: dict[str, tuple[list[str], list[FileEntry]]] = {}

    @classmethod
    def open(cls, path: Path) -> Backup:
        """Read the plists and the manifest (Manifest.mbdb or Manifest.db). Raises BackupError."""
        backup = cls(read_info(Path(path)))
        backup._ensure_loaded()
        return backup

    def __repr__(self) -> str:
        return f"Backup({self.info.device_name!r}, {str(self.info.path)!r})"

    def entries(self) -> list[FileEntry]:
        """Every manifest record. Loaded once and cached."""
        self._ensure_loaded()
        return list(self._entries)

    def find(self, domain: str, relative_path: str) -> FileEntry | None:
        """Exact lookup, e.g. find("HomeDomain", "Library/SMS/sms.db")."""
        self._ensure_loaded()
        return self._by_key.get((domain, relative_path))

    def under(self, domain: str, path_prefix: str = "") -> Iterable[FileEntry]:
        """Entries in `domain` whose relative_path starts with `path_prefix`."""
        self._ensure_loaded()
        bucket = self._by_domain.get(domain)
        if bucket is None:
            return []
        paths, entries = bucket
        if not path_prefix:
            return list(entries)
        start = end = bisect_left(paths, path_prefix)
        while end < len(paths) and paths[end].startswith(path_prefix):
            end += 1
        return entries[start:end]

    def local_path(self, entry: FileEntry) -> Path:
        """A plaintext copy of the file on disk that callers may read but must not modify.

        Unencrypted backups return the backup's own data file. The file usually has no
        extension. Raises MissingFileError, or EncryptedBackupError until unlocking is supported.
        """
        if self.info.is_encrypted:
            raise EncryptedBackupError(f"{self.info.path} is encrypted; unlocking arrives in iteration 2")
        file_id = entry.file_id.lower()
        if not _FILE_ID.fullmatch(file_id):
            raise MissingFileError(f"Manifest has an invalid file ID {entry.file_id!r} for {_describe(entry)}")

        nested = self.info.path / file_id[:2] / file_id  # iOS 10+
        flat = self.info.path / file_id  # iOS 4–9
        candidates = (flat, nested) if self.info.manifest_format is ManifestFormat.MBDB else (nested, flat)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise MissingFileError(f"{_describe(entry)} is in the manifest but its data file {file_id} is not in the backup")

    def open_file(self, entry: FileEntry) -> BinaryIO:
        path = self.local_path(entry)
        try:
            return path.open("rb")
        except FileNotFoundError as exc:  # deleted between the check and the open
            raise MissingFileError(f"{_describe(entry)}: {exc}") from exc

    def read_bytes(self, entry: FileEntry) -> bytes:
        path = self.local_path(entry)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise MissingFileError(f"{_describe(entry)}: {exc}") from exc

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            if self.info.is_encrypted:
                # The mbdb of an old encrypted backup is readable, but nothing behind it is until
                # iteration 2 adds a password. Refuse uniformly so the UI shows one locked state.
                raise EncryptedBackupError(f"{self.info.path} is encrypted; unlocking arrives in iteration 2")
            self._index(read_manifest(self.info.path, self.info.manifest_format))
            self._loaded = True  # last: other threads only read the indexes once this is set

    def _index(self, entries: list[FileEntry]) -> None:
        by_key: dict[tuple[str, str], FileEntry] = {}
        grouped: dict[str, list[FileEntry]] = defaultdict(list)
        for entry in entries:
            by_key.setdefault((entry.domain, entry.relative_path), entry)
            grouped[entry.domain].append(entry)
        by_domain: dict[str, tuple[list[str], list[FileEntry]]] = {}
        for domain, items in grouped.items():
            items.sort(key=lambda e: e.relative_path)
            by_domain[domain] = ([e.relative_path for e in items], items)
        self._entries = tuple(entries)
        self._by_key = by_key
        self._by_domain = by_domain


def _describe(entry: FileEntry) -> str:
    return f"{entry.domain}/{entry.relative_path}"


__all__ = ["Backup", "BackupError", "EncryptedBackupError", "MissingFileError"]
