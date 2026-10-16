"""The fast MBFile reader must agree with the plistlib path wherever it answers at all."""

from __future__ import annotations

import plistlib
import sqlite3
from pathlib import Path
from plistlib import UID

import pytest

from iphone_backup_viewer.core.manifest._fast_mbfile import read_mbfile
from iphone_backup_viewer.core.manifest.sqlite import _read_mbfile_with_plistlib
from tests.fixtures import fake_backup as fb

KEY = b"\x03\x00\x00\x00" + bytes(range(40))


def _archive(mbfile: dict, *, padding: int = 0, extra: list | None = None) -> bytes:
    """An NSKeyedArchiver archive with the MBFile at $objects[1 + padding]."""
    objects = ["$null"] + [f"pad-{i}" for i in range(padding)] + [mbfile] + (extra or [])
    root = UID(1 + padding)
    return plistlib.dumps(
        {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": root}, "$objects": objects},
        fmt=plistlib.FMT_BINARY,
    )


def _agrees(blob: bytes, *, must_be_fast: bool = True) -> None:
    fast = read_mbfile(blob)
    if must_be_fast:
        assert fast is not None, "fast path declined a normal blob"
    if fast is not None:
        assert fast == _read_mbfile_with_plistlib(blob)


@pytest.mark.parametrize("size", [0, 1, 200, 300, 70_000, 2**33, 2**62])
@pytest.mark.parametrize("key", [None, KEY])
def test_int_widths_and_keys(size: int, key: bytes | None) -> None:
    _agrees(fb.mbfile_blob("Media/DCIM/100APPLE/IMG_0001.JPG", size=size, mtime=1_600_000_000, encryption_key=key))


def test_symlink_target_and_unicode_path() -> None:
    _agrees(fb.mbfile_blob("Library/Notes/café ✓.txt", size=3, mtime=1, mode=0o120755, target="/x/ÿ"))


def test_many_objects_need_two_byte_refs() -> None:
    blob = _archive({"Size": 5, "LastModified": 7, "Mode": 33188, "ProtectionClass": 3, "$class": UID(2)}, padding=400)
    assert blob[-32 + 7] == 2  # object ref size in the trailer
    assert read_mbfile(blob) == (5, 7, 3, 33188, None)
    _agrees(blob)


def test_values_behind_uids_are_followed() -> None:
    blob = _archive({"Size": UID(2), "LastModified": 9, "EncryptionKey": UID(3)}, extra=[12345, {"NS.data": UID(4)}, KEY])
    assert read_mbfile(blob) == (12345, 9, None, None, KEY)
    _agrees(blob)


def test_wrong_types_become_none_like_plistlib() -> None:
    blob = _archive({"Size": "big", "LastModified": 1.5, "ProtectionClass": True, "Mode": b"\x01"})
    _agrees(blob)
    assert _read_mbfile_with_plistlib(blob) == (None, None, None, None, None)


def test_unusual_shapes_decline_to_plistlib() -> None:
    assert read_mbfile(b"") is None
    assert read_mbfile(b"bplist00" + b"\x00" * 40) is None
    assert read_mbfile(plistlib.dumps({"a": 1}, fmt=plistlib.FMT_BINARY)) is None
    assert read_mbfile(plistlib.dumps({"a": 1})) is None  # XML
    no_size = _archive({"LastModified": 1})
    assert read_mbfile(no_size) is None and _read_mbfile_with_plistlib(no_size) == (None, 1, None, None, None)
    data_key = _archive({"Size": 1, "LastModified": 1, "EncryptionKey": UID(2)}, extra=[KEY])
    _agrees(data_key, must_be_fast=False)  # NSData stored bare: only plistlib's path handles it
    assert _read_mbfile_with_plistlib(data_key)[4] == KEY


def test_every_blob_in_the_fixture_backup(modern_path: Path) -> None:
    conn = sqlite3.connect(modern_path / "Manifest.db")
    try:
        blobs = [row[0] for row in conn.execute("SELECT file FROM Files")]
    finally:
        conn.close()
    assert blobs
    for blob in blobs:
        _agrees(blob)


@pytest.mark.parametrize("cut", [8, 20, 60, 100, -40, -33, -1])
def test_truncated_blobs_never_raise(cut: int) -> None:
    blob = fb.mbfile_blob("x", size=1, mtime=1, encryption_key=KEY)
    read_mbfile(blob[:cut])
