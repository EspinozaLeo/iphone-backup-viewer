from __future__ import annotations

import hashlib
import plistlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from iphone_backup_viewer.core.errors import BackupError
from iphone_backup_viewer.core.manifest import read_manifest_db, read_mbdb
from iphone_backup_viewer.core.manifest.mbdb import parse_mbdb
from iphone_backup_viewer.core.models import FileKind
from tests.fixtures import fake_backup as fb

UTC = timezone.utc
WHEN = datetime(2016, 2, 29, 12, 0, 0, tzinfo=UTC)
KEY = b"\x03\x00\x00\x00" + bytes(range(40))  # class 3 prefix + 40-byte wrapped key

SAMPLE = [
    fb.FakeFile("CameraRollDomain", "Media/DCIM", fb.DIRECTORY, modified=WHEN),
    fb.FakeFile("CameraRollDomain", "Media/DCIM/100APPLE/IMG_0001.JPG", data=b"jpeg" * 100, modified=WHEN),
    fb.FakeFile("HomeDomain", "Library/SMS/Drafts", fb.SYMLINK, modified=WHEN, link_target="/somewhere"),
    fb.FakeFile(
        "HomeDomain",
        "Library/Keychain/secret.db",
        data=b"s" * 7,
        modified=WHEN,
        protection_class=4,
        encryption_key=KEY,
        properties=((b"com.apple.xattr", b"\x00\x01binary"), ("name", "value")),
    ),
    fb.FakeFile("HomeDomain", "", fb.DIRECTORY, modified=WHEN),
]


def _check_roundtrip(entries) -> None:
    by_path = {(e.domain, e.relative_path): e for e in entries}
    assert len(entries) == len(SAMPLE)
    for fake in SAMPLE:
        entry = by_path[(fake.domain, fake.relative_path)]
        assert entry.file_id == fake.file_id
        assert entry.kind.value == fake.kind
        assert entry.size == fake.size
        assert entry.modified == WHEN
        assert entry.protection_class == fake.protection_class
        assert entry.wrapped_key == fake.encryption_key


# ---- mbdb ----------------------------------------------------------------------------------


def test_mbdb_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "Manifest.mbdb"
    path.write_bytes(fb.mbdb_bytes(SAMPLE))
    _check_roundtrip(read_mbdb(path))


def test_mbdb_file_id_is_sha1_of_domain_dash_path() -> None:
    entry = parse_mbdb(fb.mbdb_bytes([fb.FakeFile("HomeDomain", "Library/SMS/sms.db")]))[0]
    assert entry.file_id == "3d0d7e5fb2ce288813306e4d4636395e047a3d28"


def test_mbdb_bad_header_raises() -> None:
    with pytest.raises(BackupError):
        parse_mbdb(b"mbdx\x02\x00" + b"\x00" * 50)
    with pytest.raises(BackupError):
        parse_mbdb(b"")


def test_mbdb_header_only_is_empty() -> None:
    assert parse_mbdb(fb.MBDB_HEADER) == []


@pytest.mark.parametrize("cut", [1, 2, 5, 30, 41])
def test_mbdb_truncated_record_keeps_what_was_parsed(cut: int) -> None:
    data = fb.mbdb_bytes(SAMPLE)
    last = fb.mbdb_bytes(SAMPLE[-1:])[len(fb.MBDB_HEADER) :]
    truncated = data[: len(data) - len(last) + cut]  # last record cut short
    entries = parse_mbdb(truncated)
    assert [e.relative_path for e in entries] == [f.relative_path for f in SAMPLE[:-1]]


def test_mbdb_non_utf8_path_hashes_raw_bytes() -> None:
    raw_path = b"Media/DCIM/100APPLE/caf\xe9.JPG"  # Latin-1, not UTF-8
    record = fb.mbdb_record(b"CameraRollDomain", raw_path, mode=0o100644, size=3, mtime=int(WHEN.timestamp()))
    entry = parse_mbdb(fb.MBDB_HEADER + record)[0]
    assert entry.file_id == hashlib.sha1(b"CameraRollDomain-" + raw_path).hexdigest()
    assert entry.relative_path.startswith("Media/DCIM/100APPLE/caf")


def test_mbdb_null_strings_and_zero_mtime() -> None:
    record = fb.mbdb_record(b"HomeDomain", None, mode=0o040755, size=0, mtime=0)
    entry = parse_mbdb(fb.MBDB_HEADER + record)[0]
    assert entry.relative_path == ""
    assert entry.kind is FileKind.DIRECTORY
    assert entry.modified is None
    assert entry.wrapped_key is None


# ---- Manifest.db ---------------------------------------------------------------------------


def test_manifest_db_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "Manifest.db"
    fb.write_manifest_db(path, [fb.manifest_db_row(f) for f in SAMPLE])
    _check_roundtrip(read_manifest_db(path))


def test_manifest_db_garbled_blobs_do_not_kill_the_load(tmp_path: Path) -> None:
    good = fb.manifest_db_row(SAMPLE[1])
    weird_root = plistlib.dumps({"$top": {"root": plistlib.UID(99)}, "$objects": ["$null"]}, fmt=plistlib.FMT_BINARY)
    wrong_types = fb.mbfile_blob("x", size=5, mtime=1).replace(b"Size", b"Sizf")
    rows = [
        good,
        ("a" * 40, "HomeDomain", "garbage", 1, b"bplist00 definitely not"),
        ("b" * 40, "HomeDomain", "null", 1, None),
        ("c" * 40, "HomeDomain", "dangling", 1, weird_root),
        ("d" * 40, "HomeDomain", "renamed key", 1, wrong_types),
        ("e" * 40, "HomeDomain", "xml", 1, plistlib.dumps({"a": 1})),
    ]
    path = tmp_path / "Manifest.db"
    fb.write_manifest_db(path, rows)
    entries = {e.relative_path: e for e in read_manifest_db(path)}
    assert entries[SAMPLE[1].relative_path].size == SAMPLE[1].size
    for name in ("garbage", "null", "dangling", "xml"):
        assert entries[name].size == 0
        assert entries[name].modified is None
        assert entries[name].kind is FileKind.FILE
    assert entries["renamed key"].size == 0
    assert entries["renamed key"].modified is not None  # the fields that parsed are kept


def test_manifest_db_unknown_flags_fall_back_to_mode(tmp_path: Path) -> None:
    blob = fb.mbfile_blob("d", size=0, mtime=1, mode=0o040755)
    path = tmp_path / "Manifest.db"
    fb.write_manifest_db(path, [("f" * 40, "HomeDomain", "d", 0, blob)])
    assert read_manifest_db(path)[0].kind is FileKind.DIRECTORY


def test_manifest_db_that_is_not_sqlite_raises(tmp_path: Path) -> None:
    path = tmp_path / "Manifest.db"
    path.write_bytes(b"\x8a\x13" * 4096)  # what an encrypted Manifest.db looks like
    with pytest.raises(BackupError):
        read_manifest_db(path)


def test_manifest_db_without_files_table_raises(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "Manifest.db"
    sqlite3.connect(path).close()
    with pytest.raises(BackupError):
        read_manifest_db(path)


def test_manifest_db_path_with_uri_special_characters(tmp_path: Path) -> None:
    folder = tmp_path / "my backups #1 50%20 & odd"
    folder.mkdir()
    path = folder / "Manifest.db"
    fb.write_manifest_db(path, [fb.manifest_db_row(SAMPLE[1])])
    assert len(read_manifest_db(path)) == 1


def test_manifest_db_read_leaves_no_files_behind(tmp_path: Path) -> None:
    path = tmp_path / "Manifest.db"
    fb.write_manifest_db(path, [fb.manifest_db_row(f) for f in SAMPLE])
    before = sorted(p.name for p in tmp_path.iterdir())
    read_manifest_db(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
