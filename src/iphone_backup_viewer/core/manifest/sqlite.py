"""iOS 10+ Manifest.db: SQLite table Files(fileID, domain, relativePath, flags, file).

`flags` is 1 for a file, 2 for a directory and 4 for a symlink. `file` is an NSKeyedArchiver binary
plist whose root object (an MBFile) holds Size, LastModified (unix seconds), ProtectionClass, Mode
and, in encrypted backups, EncryptionKey (a reference to an NSData dict holding `NS.data`).

Everything is read in one pass and the connection is closed before returning, so no SQLite handle
outlives the call or crosses threads. The database is opened read-only: nothing is ever written
next to it, not even a journal.
"""

from __future__ import annotations

import logging
import plistlib
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

from .._time import utc_from_unix
from ..errors import BackupError
from ..models import FileEntry, FileKind
from ._fast_mbfile import read_mbfile
from ._kinds import kind_from_mode

log = logging.getLogger(__name__)

_FLAG_KINDS = {1: FileKind.FILE, 2: FileKind.DIRECTORY, 4: FileKind.SYMLINK}
_QUERY = "SELECT fileID, domain, relativePath, flags, file FROM Files"
# "immutable" skips locking and journal checks entirely. It is only the fallback, for databases
# that a plain read-only open refuses (for example a leftover hot journal we must not roll back).
_OPEN_MODES = ("mode=ro", "mode=ro&immutable=1")


class _FileMeta(NamedTuple):
    size: int
    modified: datetime | None
    protection_class: int | None
    wrapped_key: bytes | None
    mode: int | None


_NO_META = _FileMeta(0, None, None, None, None)


def read_manifest_db(path: Path) -> list[FileEntry]:
    rows = _read_rows(path)
    entries: list[FileEntry] = []
    garbled = 0
    for file_id, domain, relative_path, flags, blob in rows:
        if not file_id:
            continue
        meta = _parse_file_blob(blob)
        if meta is None:
            garbled += 1
            meta = _NO_META
        kind = _FLAG_KINDS.get(flags) or kind_from_mode(meta.mode) or FileKind.FILE
        entries.append(
            FileEntry(
                file_id=str(file_id).lower(),
                domain=domain or "",
                relative_path=relative_path or "",
                kind=kind,
                size=meta.size,
                modified=meta.modified,
                protection_class=meta.protection_class,
                wrapped_key=meta.wrapped_key,
            )
        )
    if garbled:
        log.warning("%s: %d of %d records had an unreadable 'file' blob (size/time unknown)", path, garbled, len(rows))
    return entries


def _sqlite_uri(path: Path, params: str) -> str:
    posix = path.resolve().as_posix()  # "C:/a b/Manifest.db", "//server/share/...", "/home/..."
    if not posix.startswith("/"):
        posix = "/" + posix
    return "file://" + quote(posix, safe="/:") + "?" + params


def _read_rows(path: Path) -> list[tuple]:
    if not path.is_file():
        raise BackupError(f"{path} does not exist")
    last_error: Exception | None = None
    for params in _OPEN_MODES:
        try:
            conn = sqlite3.connect(_sqlite_uri(path, params), uri=True)
        except sqlite3.Error as exc:
            last_error = exc
            continue
        with closing(conn):
            # Paths are not guaranteed to be valid UTF-8; never let one row fail the whole read.
            conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
            try:
                return conn.execute(_QUERY).fetchall()
            except sqlite3.Error as exc:
                last_error = exc
    raise BackupError(f"Cannot read {path}: {last_error}") from last_error


def _deref(objects: list, value: object) -> object:
    """Follow an NSKeyedArchiver UID into $objects. "$null" becomes None."""
    for _ in range(4):  # a reference to a reference is legal but rare; never loop forever
        if not isinstance(value, plistlib.UID):
            break
        index = value.data
        if not 0 <= index < len(objects):
            return None
        value = objects[index]
    if value == "$null":
        return None
    return value


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


_RawFields = tuple[int | None, int | None, int | None, int | None, bytes | None]


def _parse_file_blob(blob: object) -> _FileMeta | None:
    """Size, mtime, protection class, wrapped key and mode from one `file` blob. None if unreadable."""
    if not isinstance(blob, (bytes, bytearray, memoryview)) or not blob:
        return None
    blob = bytes(blob)
    raw = read_mbfile(blob)  # fast path for the usual shape
    if raw is None:
        raw = _read_mbfile_with_plistlib(blob)
    if raw is None:
        return None
    size, mtime, protection_class, mode, wrapped_key = raw
    return _FileMeta(
        size=size if size is not None and size >= 0 else 0,
        modified=utc_from_unix(mtime),
        protection_class=protection_class,
        # Raw, including the 4-byte little-endian class prefix; the crypto layer strips it.
        wrapped_key=wrapped_key,
        mode=mode,
    )


def _read_mbfile_with_plistlib(blob: bytes) -> _RawFields | None:
    """The general path: any valid archive, whatever its encoding quirks. Same tuple as read_mbfile."""
    try:
        archive = plistlib.loads(blob, fmt=plistlib.FMT_BINARY)
        objects = archive["$objects"]
        root = _deref(objects, archive["$top"]["root"])
    except Exception:  # plistlib raises a zoo of types on bad input; any of them means "garbled"
        return None
    if not isinstance(objects, list) or not isinstance(root, dict):
        return None

    key = _deref(objects, root.get("EncryptionKey"))
    if isinstance(key, dict):
        key = _deref(objects, key.get("NS.data"))
    return (
        _as_int(_deref(objects, root.get("Size"))),
        _as_int(_deref(objects, root.get("LastModified"))),
        _as_int(_deref(objects, root.get("ProtectionClass"))),
        _as_int(_deref(objects, root.get("Mode"))),
        bytes(key) if isinstance(key, (bytes, bytearray)) and key else None,
    )
