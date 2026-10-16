"""Shared fixtures. The backups are generated once per session and must be treated as read-only."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures import fake_backup


@pytest.fixture(scope="session")
def backups_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A folder holding all three synthetic backups, like MobileSync/Backup."""
    root = tmp_path_factory.mktemp("MobileSync") / "Backup"
    fake_backup.make_all(root)
    return root


@pytest.fixture(scope="session")
def modern_path(backups_root: Path) -> Path:
    return backups_root / fake_backup.MODERN_UDID


@pytest.fixture(scope="session")
def legacy_path(backups_root: Path) -> Path:
    return backups_root / fake_backup.LEGACY_UDID


@pytest.fixture(scope="session")
def encrypted_path(backups_root: Path) -> Path:
    return backups_root / fake_backup.ENCRYPTED_UDID


@pytest.fixture(scope="session")
def modern_backup(modern_path: Path):
    from iphone_backup_viewer.core.backup import Backup

    return Backup.open(modern_path)


@pytest.fixture(scope="session")
def legacy_backup(legacy_path: Path):
    from iphone_backup_viewer.core.backup import Backup

    return Backup.open(legacy_path)
