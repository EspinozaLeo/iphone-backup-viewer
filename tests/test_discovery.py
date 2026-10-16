from __future__ import annotations

import plistlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from iphone_backup_viewer.core import discovery
from iphone_backup_viewer.core.errors import BackupError
from iphone_backup_viewer.core.models import ManifestFormat
from tests.fixtures import fake_backup as fb

UTC = timezone.utc


def _copy_plists(src: Path, dest: Path, *names: str) -> Path:
    dest.mkdir(parents=True)
    for name in names:
        shutil.copy2(src / name, dest / name)
    return dest


def test_modern_backup_summary(modern_path: Path) -> None:
    info = discovery.read_info(modern_path)
    assert info.path == modern_path
    assert info.udid == fb.MODERN_UDID
    assert info.device_name == fb.MODERN.device_name
    assert info.product_type == "iPhone15,4"
    assert info.ios_version == "17.5.1"
    assert info.last_backup == fb.MODERN.last_backup
    assert info.last_backup.tzinfo is not None and info.last_backup.utcoffset().total_seconds() == 0
    assert info.is_encrypted is False
    assert info.manifest_format is ManifestFormat.SQLITE


def test_legacy_backup_summary(legacy_path: Path) -> None:
    info = discovery.read_info(legacy_path)
    assert info.udid.lower() == fb.LEGACY_UDID  # Info.plist's "Unique Identifier" is upper-case
    assert info.ios_version == "7.1.2"
    assert info.last_backup == fb.LEGACY.last_backup
    assert info.manifest_format is ManifestFormat.MBDB
    assert info.is_encrypted is False


def test_encrypted_flag_comes_from_manifest_plist(encrypted_path: Path) -> None:
    info = discovery.read_info(encrypted_path)
    assert info.is_encrypted is True
    assert info.manifest_format is ManifestFormat.SQLITE


def test_missing_info_plist_falls_back_to_manifest_lockdown(modern_path: Path, tmp_path: Path) -> None:
    backup = _copy_plists(modern_path, tmp_path / "copy", "Manifest.plist", "Status.plist", "Manifest.db")
    info = discovery.read_info(backup)
    assert info.device_name == fb.MODERN.device_name
    assert info.product_type == fb.MODERN.product_type
    assert info.ios_version == fb.MODERN.ios_version
    assert info.udid == fb.MODERN_UDID.lower()  # Lockdown.UniqueDeviceID
    assert info.last_backup == fb.MODERN.last_backup  # Manifest.plist Date


def test_missing_manifest_plist_means_not_encrypted(encrypted_path: Path, tmp_path: Path) -> None:
    backup = _copy_plists(encrypted_path, tmp_path / "copy", "Info.plist", "Status.plist")
    info = discovery.read_info(backup)
    assert info.device_name == fb.ENCRYPTED.device_name
    assert info.is_encrypted is False
    assert info.manifest_format is None


def test_only_status_plist_uses_folder_name_and_status_date(tmp_path: Path) -> None:
    backup = tmp_path / "f00dfeed"
    backup.mkdir()
    when = datetime(2019, 4, 1, 8, 0, 0)  # naive, as plistlib returns it
    (backup / "Status.plist").write_bytes(plistlib.dumps({"Date": when, "Version": "3.3"}, fmt=plistlib.FMT_BINARY))
    info = discovery.read_info(backup)
    assert info.udid == "f00dfeed"
    assert info.device_name == "Unknown device"
    assert info.last_backup == when.replace(tzinfo=UTC)
    assert info.manifest_format is None


def test_corrupt_plist_is_tolerated(modern_path: Path, tmp_path: Path) -> None:
    backup = _copy_plists(modern_path, tmp_path / "copy", "Manifest.plist", "Manifest.db")
    (backup / "Info.plist").write_bytes(b"<?xml this is not a plist")
    info = discovery.read_info(backup)
    assert info.device_name == fb.MODERN.device_name


def test_mbdb_detection_and_db_preference(tmp_path: Path) -> None:
    backup = tmp_path / "b"
    backup.mkdir()
    (backup / "Manifest.mbdb").write_bytes(fb.MBDB_HEADER)
    assert discovery.read_info(backup).manifest_format is ManifestFormat.MBDB
    (backup / "Manifest.db").write_bytes(b"")
    assert discovery.read_info(backup).manifest_format is ManifestFormat.SQLITE


def test_not_a_backup_raises(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        discovery.read_info(tmp_path)
    (tmp_path / "notes.txt").write_text("hello")
    with pytest.raises(BackupError):
        discovery.read_info(tmp_path / "notes.txt")
    with pytest.raises(BackupError):
        discovery.read_info(tmp_path / "does-not-exist")


def test_scan_root_newest_first_and_skips_non_backups(backups_root: Path, tmp_path: Path) -> None:
    root = tmp_path / "Backup"
    shutil.copytree(backups_root, root)
    (root / "random folder").mkdir()
    (root / "random folder" / "photo.jpg").write_bytes(b"x")
    (root / "stray.plist").write_bytes(b"")
    snapshot = root / "Snapshot"  # an in-progress backup's staging dir
    _copy_plists(root / fb.MODERN_UDID, snapshot, "Info.plist", "Manifest.plist", "Status.plist")

    infos = discovery.scan(root)
    assert [i.path.name for i in infos] == [fb.ENCRYPTED_UDID, fb.MODERN_UDID, fb.LEGACY_UDID]  # 2026, 2025, 2014


def test_scan_unknown_dates_sort_last(tmp_path: Path) -> None:
    (tmp_path / "nodate").mkdir()
    (tmp_path / "nodate" / "Manifest.db").write_bytes(b"")
    dated = tmp_path / "dated"
    dated.mkdir()
    plist = {"Date": datetime(2001, 1, 1)}
    (dated / "Status.plist").write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_BINARY))
    assert [i.path.name for i in discovery.scan(tmp_path)] == ["dated", "nodate"]


def test_scan_accepts_a_single_backup_folder(modern_path: Path) -> None:
    infos = discovery.scan(modern_path)
    assert len(infos) == 1 and infos[0].udid == fb.MODERN_UDID


def test_scan_of_missing_or_empty_root_is_empty(tmp_path: Path) -> None:
    assert discovery.scan(tmp_path / "nope") == []
    assert discovery.scan(tmp_path) == []


def test_default_backup_roots_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    appdata = tmp_path / "Roaming"
    profile = tmp_path / "Profile"
    itunes = appdata / "Apple Computer" / "MobileSync" / "Backup"
    itunes.mkdir(parents=True)
    profile.mkdir()  # Apple Devices folder absent: must be left out
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("USERPROFILE", str(profile))
    assert discovery.default_backup_roots() == [itunes]

    store = profile / "Apple" / "MobileSync" / "Backup"
    store.mkdir(parents=True)
    assert discovery.default_backup_roots() == [itunes, store]
