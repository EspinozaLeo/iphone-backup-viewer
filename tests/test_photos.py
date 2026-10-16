from __future__ import annotations

from datetime import datetime, timezone

import pytest

from iphone_backup_viewer.core.backup import Backup
from iphone_backup_viewer.core.models import FileEntry, FileKind, MediaKind
from iphone_backup_viewer.extractors.photos import camera_roll
from tests.fixtures import fake_backup as fb

UTC = timezone.utc


@pytest.fixture(params=["modern", "legacy"])
def roll(request, modern_backup: Backup, legacy_backup: Backup):
    return camera_roll(modern_backup if request.param == "modern" else legacy_backup)


def test_camera_roll_contents_and_order(roll) -> None:
    assert [item.entry.name for item in roll] == fb.EXPECTED_CAMERA_ROLL
    taken = [item.taken for item in roll]
    assert taken == sorted(taken)
    assert all(item.taken == item.entry.modified for item in roll)


def test_live_photo_is_one_item(roll) -> None:
    live = next(item for item in roll if item.entry.relative_path == fb.LIVE_PHOTO)
    assert live.kind is MediaKind.PHOTO
    assert live.live_video is not None and live.live_video.relative_path == fb.LIVE_VIDEO
    assert fb.LIVE_VIDEO not in {item.entry.relative_path for item in roll}
    assert sum(1 for item in roll if item.live_video is not None) == 1


def test_kinds_and_filtering(roll) -> None:
    kinds = {item.entry.relative_path: item.kind for item in roll}
    assert kinds[fb.PORTRAIT_VIDEO] is MediaKind.VIDEO
    assert kinds[fb.SCREENSHOT] is MediaKind.PHOTO
    assert fb.SIDECAR not in kinds
    assert all(path.startswith("Media/DCIM/") for path in kinds)
    assert fb.MISSING_PHOTO in kinds and fb.CORRUPT_PHOTO in kinds  # the UI shows placeholders
    assert all(item.entry.kind is FileKind.FILE for item in roll)


def test_media_domain_duplicates_are_dropped(legacy_backup: Backup) -> None:
    assert legacy_backup.find("MediaDomain", "Media/DCIM/100APPLE/IMG_0001.JPG") is not None
    items = [i for i in camera_roll(legacy_backup) if i.entry.name == "IMG_0001.JPG"]
    assert len(items) == 1 and items[0].entry.domain == "CameraRollDomain"


# ---- edge cases on a hand-built manifest ---------------------------------------------------


class _StubBackup:
    """Just enough of Backup for camera_roll: under() over a fixed list."""

    def __init__(self, entries: list[FileEntry]) -> None:
        self._entries = entries

    def under(self, domain: str, prefix: str = ""):
        return [e for e in self._entries if e.domain == domain and e.relative_path.startswith(prefix)]


def _entry(path: str, when: datetime | None, kind=FileKind.FILE, domain="CameraRollDomain") -> FileEntry:
    return FileEntry(file_id="0" * 40, domain=domain, relative_path=path, kind=kind, size=1, modified=when)


T1 = datetime(2018, 1, 1, tzinfo=UTC)
T2 = datetime(2019, 1, 1, tzinfo=UTC)


def test_pairing_is_case_insensitive_and_per_folder() -> None:
    items = camera_roll(
        _StubBackup(
            [
                _entry("Media/DCIM/100APPLE/IMG_0001.HEIC", T1),
                _entry("Media/DCIM/100APPLE/img_0001.mov", T1),
                _entry("Media/DCIM/101APPLE/IMG_0001.MOV", T2),  # same stem, other folder: its own video
                _entry("Media/DCIM/100APPLE/IMG_0002.JPG", T2),
                _entry("Media/DCIM/100APPLE/IMG_0002.MP4", T2),  # only .mov is Live Photo motion
            ]
        )
    )
    by_path = {i.entry.relative_path: i for i in items}
    assert by_path["Media/DCIM/100APPLE/IMG_0001.HEIC"].live_video.relative_path == "Media/DCIM/100APPLE/img_0001.mov"
    assert by_path["Media/DCIM/101APPLE/IMG_0001.MOV"].kind is MediaKind.VIDEO
    assert by_path["Media/DCIM/100APPLE/IMG_0002.JPG"].live_video is None
    assert by_path["Media/DCIM/100APPLE/IMG_0002.MP4"].kind is MediaKind.VIDEO
    assert len(items) == 4


def test_ties_break_on_path_and_unknown_times_go_last() -> None:
    items = camera_roll(
        _StubBackup(
            [
                _entry("Media/DCIM/100APPLE/IMG_0003.JPG", None),
                _entry("Media/DCIM/100APPLE/IMG_0002.JPG", T1),
                _entry("Media/DCIM/100APPLE/IMG_0001.JPG", T1),
                _entry("Media/DCIM/100APPLE/IMG_0000.JPG", T2),
            ]
        )
    )
    assert [i.entry.name for i in items] == ["IMG_0001.JPG", "IMG_0002.JPG", "IMG_0000.JPG", "IMG_0003.JPG"]


def test_non_files_sidecars_and_hidden_files_are_skipped() -> None:
    items = camera_roll(
        _StubBackup(
            [
                _entry("Media/DCIM/100APPLE", T1, FileKind.DIRECTORY),
                _entry("Media/DCIM/100APPLE/IMG_0001.JPG", T1, FileKind.SYMLINK),
                _entry("Media/DCIM/100APPLE/IMG_0002.AAE", T1),
                _entry("Media/DCIM/100APPLE/._IMG_0003.JPG", T1),
                _entry("Media/DCIM/.MISC/Info.plist", T1),
                _entry("Media/DCIM/100APPLE/IMG_0004.JPG", T1, domain="MediaDomain"),
            ]
        )
    )
    assert [i.entry.relative_path for i in items] == ["Media/DCIM/100APPLE/IMG_0004.JPG"]


def test_empty_backup_has_empty_roll() -> None:
    assert camera_roll(_StubBackup([])) == []
