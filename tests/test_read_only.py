"""Principle 1: nothing is ever written inside a backup folder, whatever the viewer reads."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from iphone_backup_viewer.core.backup import Backup
from iphone_backup_viewer.core.errors import MissingFileError
from iphone_backup_viewer.extractors.photos import camera_roll
from iphone_backup_viewer.media.thumbnails import ThumbnailCache
from tests.fixtures import fake_backup as fb


def _snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in folder.rglob("*")
    }


@pytest.mark.parametrize("which", ["modern", "legacy"])
def test_nothing_is_ever_written_inside_a_backup(which: str, backups_root: Path, tmp_path: Path) -> None:
    folder = tmp_path / which
    shutil.copytree(backups_root / (fb.MODERN_UDID if which == "modern" else fb.LEGACY_UDID), folder)
    before = _snapshot(folder)

    backup = Backup.open(folder)
    cache = ThumbnailCache(tmp_path / "cache")
    for item in camera_roll(backup):
        cache.get(backup, item.entry)
        try:
            backup.read_bytes(item.entry)
        except MissingFileError:
            pass

    assert _snapshot(folder) == before
