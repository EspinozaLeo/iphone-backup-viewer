"""iOS 4–9 Manifest.mbdb: a flat binary list of file records. Plaintext even in encrypted backups.

Layout (all integers big-endian):

    header       b"mbdb\\x05\\x00"
    per record   domain, path, link_target, data_hash, encryption_key   strings (see below)
                 mode u16, inode u64, uid u32, gid u32,
                 mtime u32, atime u32, ctime u32, size u64,
                 protection_class u8, property_count u8
                 property_count × (name string, value string)

    string       u16 length followed by that many bytes; length 0xFFFF means null

The file ID is SHA1 of the raw bytes "{domain}-{path}", hashed before any text decoding so that
a path that is not valid UTF-8 still points at the right data file.
"""

from __future__ import annotations

import hashlib
import logging
import struct
from pathlib import Path

from .._time import utc_from_unix
from ..errors import BackupError
from ..models import FileEntry, FileKind
from ._kinds import kind_from_mode

log = logging.getLogger(__name__)

MBDB_HEADER = b"mbdb\x05\x00"

_NULL_STRING = 0xFFFF
_U16 = struct.Struct(">H")
# mode, inode, uid, gid, mtime, atime, ctime, size, protection class, property count
_FIXED = struct.Struct(">HQIIIIIQBB")


class _Truncated(Exception):
    pass


def read_mbdb(path: Path) -> list[FileEntry]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BackupError(f"Cannot read {path}: {exc}") from exc
    return parse_mbdb(data)


def parse_mbdb(data: bytes) -> list[FileEntry]:
    """Every record in an mbdb blob. A truncated tail is dropped with a warning, not raised."""
    if not data.startswith(MBDB_HEADER):
        raise BackupError("Not a Manifest.mbdb: unrecognised header " + repr(bytes(data[:6])))

    entries: list[FileEntry] = []
    pos = len(MBDB_HEADER)
    end = len(data)
    while pos < end:
        try:
            entry, pos = _read_record(data, pos)
        except _Truncated:
            log.warning(
                "Manifest.mbdb is truncated at byte %d of %d; keeping the %d records before it",
                pos, end, len(entries),
            )
            break
        entries.append(entry)
    return entries


def _read_string(data: bytes, pos: int) -> tuple[bytes | None, int]:
    if pos + 2 > len(data):
        raise _Truncated
    (length,) = _U16.unpack_from(data, pos)
    pos += 2
    if length == _NULL_STRING:
        return None, pos
    if pos + length > len(data):
        raise _Truncated
    return data[pos : pos + length], pos + length


def _read_record(data: bytes, pos: int) -> tuple[FileEntry, int]:
    domain, pos = _read_string(data, pos)
    path, pos = _read_string(data, pos)
    _link_target, pos = _read_string(data, pos)
    _data_hash, pos = _read_string(data, pos)
    encryption_key, pos = _read_string(data, pos)

    if pos + _FIXED.size > len(data):
        raise _Truncated
    (mode, _inode, _uid, _gid, mtime, _atime, _ctime, size, protection_class, property_count) = (
        _FIXED.unpack_from(data, pos)
    )
    pos += _FIXED.size
    for _ in range(property_count):
        _name, pos = _read_string(data, pos)
        _value, pos = _read_string(data, pos)

    domain_bytes = domain or b""
    path_bytes = path or b""
    file_id = hashlib.sha1(domain_bytes + b"-" + path_bytes).hexdigest()
    entry = FileEntry(
        file_id=file_id,
        domain=domain_bytes.decode("utf-8", "replace"),
        relative_path=path_bytes.decode("utf-8", "replace"),
        kind=kind_from_mode(mode) or FileKind.FILE,
        size=size,
        modified=utc_from_unix(mtime),
        protection_class=protection_class,
        # Raw, including the 4-byte little-endian class prefix, same as Manifest.db's EncryptionKey.
        wrapped_key=encryption_key or None,
    )
    return entry, pos
