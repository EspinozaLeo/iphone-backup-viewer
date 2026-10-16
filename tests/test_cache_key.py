from __future__ import annotations

import plistlib
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from iphone_backup_viewer.core.backup import Backup
from iphone_backup_viewer.core.cache_key import backup_cache_key
from iphone_backup_viewer.core.discovery import read_info
from iphone_backup_viewer.media.thumbnails import ThumbnailCache
from tests.fixtures import fake_backup as fb

PHOTO = "Media/DCIM/100APPLE/IMG_0001.JPG"


def test_dated_key_is_udid_plus_utc_timestamp(modern_path: Path) -> None:
    assert backup_cache_key(read_info(modern_path)) == f"{fb.MODERN_UDID}-20250601T184512Z"


def test_key_is_in_utc_whatever_the_datetime_zone(modern_path: Path) -> None:
    info = read_info(modern_path)
    shifted = replace(info, last_backup=info.last_backup.astimezone(timezone(timedelta(hours=-7))))
    assert backup_cache_key(shifted) == backup_cache_key(info)


def test_undated_key_hashes_the_folder(modern_path: Path, tmp_path: Path) -> None:
    info = replace(read_info(modern_path), last_backup=None)
    key = backup_cache_key(info)
    assert key.startswith(fb.MODERN_UDID + "-") and len(key) == len(fb.MODERN_UDID) + 13
    assert backup_cache_key(info) == key
    assert backup_cache_key(replace(info, path=tmp_path)) != key


def test_unsafe_udid_cannot_escape_the_cache(modern_path: Path) -> None:
    info = replace(read_info(modern_path), udid="../../evil\\x")
    key = backup_cache_key(info)
    assert "/" not in key and "\\" not in key and not key.startswith(".")


def _set_backup_date(backup: Path, when: datetime) -> None:
    info = plistlib.loads((backup / "Info.plist").read_bytes())
    info["Last Backup Date"] = when
    (backup / "Info.plist").write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML))


def test_archived_snapshot_of_the_same_phone_gets_its_own_thumbnails(modern_path: Path, tmp_path: Path) -> None:
    current = tmp_path / "Backup" / fb.MODERN_UDID
    archive = tmp_path / "Backup" / f"{fb.MODERN_UDID}-20120304-101010"
    shutil.copytree(modern_path, current)
    shutil.copytree(modern_path, archive)
    _set_backup_date(archive, datetime(2012, 3, 4, 10, 10, 10))
    old, new = Backup.open(archive), Backup.open(current)
    assert old.info.udid == new.info.udid

    entry = old.find("CameraRollDomain", PHOTO)
    assert entry.file_id == new.find("CameraRollDomain", PHOTO).file_id
    Image.new("RGB", (480, 360), (230, 20, 20)).save(old.local_path(entry), "JPEG")  # same file ID, other photo

    cache = ThumbnailCache(tmp_path / "cache")
    old_thumb, new_thumb = cache.get(old, entry), cache.get(new, entry)
    assert old_thumb is not None and new_thumb is not None and old_thumb != new_thumb
    with Image.open(old_thumb) as a, Image.open(new_thumb) as b:
        assert a.getpixel((128, 150))[0] > 200 and a.getpixel((128, 150))[2] < 60  # the red archive photo
        assert b.getpixel((128, 150))[2] > 60  # the current gradient, not the red one


def test_two_copies_of_one_snapshot_share_thumbnails(modern_path: Path, tmp_path: Path) -> None:
    copy = tmp_path / "external drive" / fb.MODERN_UDID
    shutil.copytree(modern_path, copy)
    original, duplicate = Backup.open(modern_path), Backup.open(copy)
    entry = original.find("CameraRollDomain", PHOTO)
    cache = ThumbnailCache(tmp_path / "cache")
    assert cache.get(original, entry) == cache.get(duplicate, entry)
