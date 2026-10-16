from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from iphone_backup_viewer.core.backup import Backup
from iphone_backup_viewer.core.discovery import read_info
from iphone_backup_viewer.core.errors import BackupError, EncryptedBackupError, MissingFileError
from iphone_backup_viewer.core.models import FileKind
from tests.fixtures import fake_backup as fb


@pytest.fixture(params=["modern", "legacy"])
def any_backup(request, modern_backup: Backup, legacy_backup: Backup) -> Backup:
    return modern_backup if request.param == "modern" else legacy_backup


def test_entries_match_what_was_written(any_backup: Backup) -> None:
    spec = fb.MODERN if any_backup.info.udid == fb.MODERN_UDID else fb.LEGACY
    written = {(f.domain, f.relative_path): f for f in fb.backup_files(spec)}
    entries = any_backup.entries()
    assert {(e.domain, e.relative_path) for e in entries} == set(written)
    for entry in entries:
        fake = written[(entry.domain, entry.relative_path)]
        assert entry.file_id == fake.file_id
        assert entry.kind.value == fake.kind
        assert entry.modified == fake.modified
        if fake.kind == fb.FILE and fake.store_data:
            assert entry.size == any_backup.local_path(entry).stat().st_size
        elif fake.kind == fb.FILE:
            assert entry.size > 0  # it existed on the phone; only the backup copy is gone
        else:
            assert entry.size == 0


def test_entries_returns_a_copy(modern_backup: Backup) -> None:
    modern_backup.entries().clear()
    assert modern_backup.entries()


def test_find_and_read(any_backup: Backup) -> None:
    sms = any_backup.find(*fb.SMS_DB)
    assert sms is not None and sms.kind is FileKind.FILE
    assert any_backup.read_bytes(sms).startswith(b"SQLite format 3\x00")
    with any_backup.open_file(sms) as fh:
        assert fh.read(6) == b"SQLite"
    assert any_backup.find("HomeDomain", "Library/SMS/nope.db") is None
    assert any_backup.find("NoSuchDomain", fb.SMS_DB[1]) is None


def test_under_is_a_prefix_range(any_backup: Backup) -> None:
    dcim = list(any_backup.under("CameraRollDomain", "Media/DCIM/"))
    assert dcim and all(e.relative_path.startswith("Media/DCIM/") for e in dcim)
    assert not any("PhotoData" in e.relative_path for e in dcim)
    assert len(list(any_backup.under("CameraRollDomain", "Media/DCIM/100APPLE/"))) == 5  # 4 JPG + AAE
    everything = list(any_backup.under("HomeDomain"))
    assert len(everything) == sum(1 for e in any_backup.entries() if e.domain == "HomeDomain")
    assert list(any_backup.under("NoSuchDomain")) == []
    assert list(any_backup.under("CameraRollDomain", "Media/DCIM/999")) == []


def test_data_file_layouts(modern_backup: Backup, legacy_backup: Backup) -> None:
    modern = modern_backup.local_path(modern_backup.find(*fb.SMS_DB))
    assert modern.parent.name == modern.name[:2] and modern.parent.parent == modern_backup.info.path
    legacy = legacy_backup.local_path(legacy_backup.find(*fb.SMS_DB))
    assert legacy.parent == legacy_backup.info.path


def test_other_layout_is_a_fallback(legacy_path: Path, tmp_path: Path) -> None:
    copy = tmp_path / "legacy"
    shutil.copytree(legacy_path, copy)
    entry = Backup.open(copy).find(*fb.SMS_DB)
    nested = copy / entry.file_id[:2] / entry.file_id
    nested.parent.mkdir()
    os.replace(copy / entry.file_id, nested)  # an mbdb backup with an iOS 10-style layout
    assert Backup.open(copy).local_path(entry) == nested


def test_missing_data_file(any_backup: Backup) -> None:
    entry = any_backup.find("CameraRollDomain", fb.MISSING_PHOTO)
    assert entry is not None
    with pytest.raises(MissingFileError):
        any_backup.local_path(entry)
    with pytest.raises(MissingFileError):
        any_backup.read_bytes(entry)
    with pytest.raises(MissingFileError):
        any_backup.open_file(entry)


def test_directories_and_symlinks_have_no_data(any_backup: Backup) -> None:
    for path in ("Library", "Library/SMS/Drafts"):
        with pytest.raises(MissingFileError):
            any_backup.local_path(any_backup.find("HomeDomain", path))


def test_file_id_cannot_escape_the_backup(modern_backup: Backup) -> None:
    entry = modern_backup.find(*fb.SMS_DB)
    for bad in ("../" * 6 + "Windows/win.ini", "..", "zz" * 20, ""):
        with pytest.raises(MissingFileError):
            modern_backup.local_path(replace(entry, file_id=bad))


def test_encrypted_backup_refuses_to_open(encrypted_path: Path) -> None:
    with pytest.raises(EncryptedBackupError):
        Backup.open(encrypted_path)
    lazy = Backup(read_info(encrypted_path))
    with pytest.raises(EncryptedBackupError):
        lazy.entries()


def test_encrypted_flag_blocks_file_access_even_when_constructed_directly(modern_path: Path) -> None:
    info = replace(read_info(modern_path), is_encrypted=True)
    backup = Backup(info)
    with pytest.raises(EncryptedBackupError):
        backup.local_path(Backup.open(modern_path).find(*fb.SMS_DB))


def test_missing_manifest_raises(modern_path: Path, tmp_path: Path) -> None:
    folder = tmp_path / "partial"
    folder.mkdir()
    shutil.copy2(modern_path / "Info.plist", folder / "Info.plist")
    with pytest.raises(BackupError):
        Backup.open(folder)


def test_not_a_backup_raises(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        Backup.open(tmp_path)


def test_lazy_construction_loads_on_first_use(modern_path: Path) -> None:
    backup = Backup(read_info(modern_path))
    assert backup.find(*fb.SMS_DB) is not None


def test_concurrent_reads(modern_backup: Backup) -> None:
    entries = [e for e in modern_backup.entries() if e.kind is FileKind.FILE and e.relative_path != fb.MISSING_PHOTO]
    expected = {e.file_id: modern_backup.read_bytes(e) for e in entries}

    def read(entry):
        return entry.file_id, modern_backup.read_bytes(entry)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read, entries * 8))
    assert all(expected[file_id] == data for file_id, data in results)


def test_concurrent_lazy_load_happens_once(modern_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from iphone_backup_viewer.core import backup as backup_module

    calls = []
    real = backup_module.read_manifest

    def counting(*args):
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(backup_module, "read_manifest", counting)
    backup = Backup(read_info(modern_path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(lambda _: len(backup.entries()), range(32)))
    assert len(set(counts)) == 1 and len(calls) == 1
