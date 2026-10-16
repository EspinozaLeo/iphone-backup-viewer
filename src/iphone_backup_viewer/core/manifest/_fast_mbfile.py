"""Fast path for the one binary-plist shape Manifest.db holds: an NSKeyedArchiver'd MBFile.

plistlib decodes the whole object graph through a BytesIO, about 25 µs per blob. Over a
300k-file backup that is most of the time it takes to open it. This reader goes straight to the five
values the viewer needs (Size, LastModified, ProtectionClass, Mode, EncryptionKey) and decodes
nothing else.

It handles only the expected shape. Anything else returns None, and sqlite.py falls back to plistlib.
So this path can make loading faster but never less tolerant.

bplist00 in brief: an object table, an offset table, then a 32-byte trailer. Each object starts with
a marker byte. Its high nibble is the type (0x1 int, 0x4 data, 0x5 ASCII string, 0x8 UID, 0xA array,
0xD dict). Its low nibble is a size, where 0xF means "an int object holding the size follows".
"""

from __future__ import annotations

import struct

_TRAILER = struct.Struct(">6xBBQQQ")  # offset size, ref size, object count, top object, table offset
_OTHER = object()  # a value of a type no MBFile field should have
_MAX_UID_HOPS = 4


def _ascii_key(name: str) -> bytes:
    """The exact bytes of a bplist ASCII string object, as CoreFoundation and plistlib write it."""
    raw = name.encode("ascii")
    if len(raw) < 15:
        return bytes([0x50 | len(raw)]) + raw
    return bytes([0x5F, 0x10, len(raw)]) + raw


_K_OBJECTS = _ascii_key("$objects")
_K_TOP = _ascii_key("$top")
_K_ROOT = _ascii_key("root")
_K_NSDATA = _ascii_key("NS.data")
_K_SIZE = _ascii_key("Size")
_K_MTIME = _ascii_key("LastModified")
_K_PROTECTION = _ascii_key("ProtectionClass")
_K_MODE = _ascii_key("Mode")
_K_KEY = _ascii_key("EncryptionKey")
_ARCHIVE_KEYS = frozenset({_K_OBJECTS, _K_TOP})
_ROOT_KEYS = frozenset({_K_ROOT})
_NSDATA_KEYS = frozenset({_K_NSDATA})
_MBFILE_KEYS = frozenset({_K_SIZE, _K_MTIME, _K_PROTECTION, _K_MODE, _K_KEY})


class _Unexpected(Exception):
    """Not the shape this fast path knows; let plistlib have a go."""


class _Uid(int):
    """An NSKeyedArchiver reference into $objects."""


class _Reader:
    __slots__ = ("data", "offsets", "rsize", "top", "objects")

    def __init__(self, data: bytes) -> None:
        if len(data) < 40 or data[:8] != b"bplist00":
            raise _Unexpected
        osize, rsize, count, top, table = _TRAILER.unpack_from(data, len(data) - 32)
        if not (1 <= osize <= 8 and 1 <= rsize <= 8) or top >= count or table + count * osize > len(data) - 32:
            raise _Unexpected
        # The whole offset table in one go; an out-of-range ref then fails as an IndexError.
        if osize == 1:
            self.offsets = data[table : table + count]
        elif osize == 2:
            self.offsets = struct.unpack_from(f">{count}H", data, table)
        else:
            self.offsets = [int.from_bytes(data[table + i * osize : table + (i + 1) * osize], "big") for i in range(count)]
        self.data, self.rsize, self.top = data, rsize, top
        self.objects = -1  # ref of the $objects array, set once found

    def _length(self, pos: int, low: int) -> tuple[int, int]:
        """(count, payload position) for a marker whose low nibble is `low`; `pos` is just after it."""
        if low != 0xF:
            return low, pos
        marker = self.data[pos]
        if marker >> 4 != 0x1:
            raise _Unexpected
        size = 1 << (marker & 0xF)
        return int.from_bytes(self.data[pos + 1 : pos + 1 + size], "big"), pos + 1 + size

    def _refs(self, pos: int, n: int):
        data, rsize = self.data, self.rsize
        if pos + n * rsize > len(data):  # a crafted count must not become a near-endless loop
            raise _Unexpected
        if rsize == 1:
            return data[pos : pos + n]  # bytes: indexing yields ints
        if rsize == 2:
            return struct.unpack_from(f">{n}H", data, pos)
        return [int.from_bytes(data[pos + i * rsize : pos + (i + 1) * rsize], "big") for i in range(n)]

    def lookup(self, ref: int, wanted: frozenset[bytes]) -> dict[bytes, int]:
        """{key bytes: value ref} for those of `wanted` present in the dict at `ref`."""
        pos = self.offsets[ref]
        marker = self.data[pos]
        if marker >> 4 != 0xD:
            raise _Unexpected
        n, pos = self._length(pos + 1, marker & 0xF)
        refs = self._refs(pos, 2 * n)
        found = {}
        for i in range(n):
            key = self._ascii(refs[i])
            if key in wanted:
                found[key] = refs[n + i]
        return found

    def _ascii(self, ref: int) -> bytes:
        """The raw bytes of an ASCII string object, comparable with _ascii_key(); b"" if not one."""
        data = self.data
        pos = self.offsets[ref]
        marker = data[pos]
        if marker >> 4 != 0x5:
            return b""
        n = marker & 0xF
        if n != 0xF:
            return data[pos : pos + 1 + n]
        if data[pos + 1] != 0x10:
            return b""
        return data[pos : pos + 3 + data[pos + 2]]

    def object_ref(self, uid: int) -> int:
        """The object-table ref of $objects[uid]."""
        pos = self.offsets[self.objects]
        marker = self.data[pos]
        if marker >> 4 != 0xA:
            raise _Unexpected
        n, pos = self._length(pos + 1, marker & 0xF)
        if not 0 <= uid < n:
            raise _Unexpected
        return self._refs(pos + uid * self.rsize, 1)[0]

    def scalar(self, ref: int):
        """int, bytes, _Uid, or _OTHER for any other type (dicts, strings, reals, bools...)."""
        data = self.data
        pos = self.offsets[ref]
        marker = data[pos]
        high, low = marker >> 4, marker & 0xF
        if high == 0x1:
            size = 1 << low
            return int.from_bytes(data[pos + 1 : pos + 1 + size], "big", signed=size >= 8)
        if high == 0x8:
            return _Uid.from_bytes(data[pos + 1 : pos + 2 + low], "big")
        if high == 0x4:
            n, start = self._length(pos + 1, low)
            return data[start : start + n]
        return _OTHER

    def resolve(self, ref: int | None):
        """The value at `ref`, following UIDs through $objects. None for absent or "$null" (UID 0)."""
        if ref is None:
            return None
        value = self.scalar(ref)
        for _ in range(_MAX_UID_HOPS):
            if not isinstance(value, _Uid):
                return value
            if value == 0:
                return None
            value = self.scalar(self.object_ref(value))
        raise _Unexpected


def read_mbfile(blob: bytes) -> tuple[int | None, int | None, int | None, int | None, bytes | None] | None:
    """(size, mtime, protection_class, mode, encryption_key), or None if this is not the usual shape.

    A field of an unexpected type comes back as None, exactly as on the plistlib path.
    """
    try:
        r = _Reader(blob)
        archive = r.lookup(r.top, _ARCHIVE_KEYS)
        r.objects = archive[_K_OBJECTS]
        root = r.scalar(r.lookup(archive[_K_TOP], _ROOT_KEYS)[_K_ROOT])
        if not isinstance(root, _Uid) or root == 0:
            raise _Unexpected
        fields = r.lookup(r.object_ref(root), _MBFILE_KEYS)
        if _K_SIZE not in fields or _K_MTIME not in fields:
            raise _Unexpected  # every real MBFile has both; maybe keys we failed to recognise

        ints = []
        for key in (_K_SIZE, _K_MTIME, _K_PROTECTION, _K_MODE):
            value = r.resolve(fields.get(key))
            ints.append(value if isinstance(value, int) and not isinstance(value, _Uid) else None)

        encryption_key = None
        key_ref = fields.get(_K_KEY)
        if key_ref is not None:
            holder = r.scalar(key_ref)
            if isinstance(holder, bytes):
                encryption_key = holder
            elif isinstance(holder, _Uid) and holder != 0:
                holder_ref = r.object_ref(holder)
                if r.scalar(holder_ref) is not _OTHER:
                    raise _Unexpected  # not the NSData dict we expect; plistlib's path copes
                raw = r.resolve(r.lookup(holder_ref, _NSDATA_KEYS).get(_K_NSDATA))
                encryption_key = raw if isinstance(raw, bytes) else None
    except (_Unexpected, IndexError, KeyError, struct.error, ValueError):
        return None
    size, mtime, protection_class, mode = ints
    return size, mtime, protection_class, mode, encryption_key or None
