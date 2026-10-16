"""Main window: backup sidebar on the left, Photos / Messages tabs on the right."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QTabWidget,
    QWidget,
)

from .. import APP_NAME
from ..core.errors import EncryptedBackupError
from ..core.models import BackupInfo
from . import icons, theme
from .backup_list import BackupList, is_incomplete
from .export import ExportController
from .formatting import count, error_text
from .media_viewer import MediaViewer
from .photo_grid import PhotoGrid
from .services import Services, ThumbnailSource
from .widgets import HairlineSplitter, MessagePanel, Spinner
from .workers import TaskRunner

log = logging.getLogger(__name__)

ENCRYPTED_MESSAGE = "Encrypted backups are supported in the next update."
MESSAGES_COMING = "Messages are coming in a later update."


class MainWindow(QMainWindow):
    """`services` defaults to the real core/extractors/media functions; tests pass fakes."""

    def __init__(self, services: Services | None = None, *, scan_on_start: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services or Services()
        self.runner = TaskRunner(self, max_threads=4)
        self.backup: Any = None  # the opened Backup shown in the grid
        self.backup_info: BackupInfo | None = None
        self._open_token = 0
        self.viewer: MediaViewer | None = None
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 820)
        self.setMinimumSize(QSize(760, 480))

        # Whole-window states: scanning, nothing found, normal
        self.scanning_page = MessagePanel(busy=True)
        self.scanning_page.show_message("Looking for backups…", "Checking the folders iTunes and the Apple Devices app use.")
        self.empty_page = MessagePanel()
        self.empty_page.show_message(
            "No iPhone backups found",
            "Backups made with iTunes or the Apple Devices app are usually kept in<br>"
            "<span style='white-space:nowrap'>%APPDATA%\\Apple Computer\\MobileSync\\Backup</span><br>"
            "If yours are somewhere else, open that folder or the backup itself.",
            icon=icons.phone_pixmap(56, theme.muted_text(self.palette())),
            button="Open folder…",
            on_click=self.open_folder,
            rich=True,
        )

        self.sidebar = BackupList()
        self.sidebar.setMinimumWidth(230)
        self.sidebar.backupSelected.connect(self._backup_selected)
        self.sidebar.openFolderRequested.connect(self.open_folder)

        self.grid = PhotoGrid()
        self.grid.activated.connect(self.open_viewer)
        self.grid.exportRequested.connect(self.export_items)
        self.grid.selectionCountChanged.connect(self._update_counts)
        self.photo_hint = MessagePanel()
        self.photo_hint.show_message("Pick a backup", "Choose a backup on the left to see its photos and videos.")
        self.photo_busy = MessagePanel(busy=True)
        self.photo_message = MessagePanel()
        self.photos = QStackedWidget()
        for page in (self.photo_hint, self.photo_busy, self.photo_message, self.grid):
            self.photos.addWidget(page)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.photos_tab = self.tabs.addTab(self.photos, "Photos")
        messages = MessagePanel()
        messages.show_message("Messages", MESSAGES_COMING)
        self.messages_tab = self.tabs.addTab(messages, "Messages")
        self.tabs.setTabEnabled(self.messages_tab, False)
        self.tabs.setTabToolTip(self.messages_tab, MESSAGES_COMING)

        self.splitter = HairlineSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.tabs)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(1, False)
        self.splitter.setSizes([280, 1000])

        self.central = QStackedWidget()
        for page in (self.scanning_page, self.empty_page, self.splitter):
            self.central.addWidget(page)
        self.setCentralWidget(self.central)

        self._build_menu()
        self._build_status_bar()
        self.export = ExportController(self, self.runner)
        if scan_on_start:
            self.scan_default_locations()
        else:
            self.central.setCurrentWidget(self.empty_page)

    # -- chrome ------------------------------------------------------------------------------------
    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.open_action = QAction("&Open folder…", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.setStatusTip("Open a backup folder, or a folder that contains backups")
        self.open_action.triggered.connect(self.open_folder)
        file_menu.addAction(self.open_action)
        self.export_action = QAction("&Export selected…", self)
        self.export_action.setShortcut(QKeySequence("Ctrl+E"))
        self.export_action.setEnabled(False)
        self.export_action.triggered.connect(self.grid.request_export)
        file_menu.addAction(self.export_action)
        file_menu.addSeparator()
        self.exit_action = QAction("E&xit", self)
        self.exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self.busy = Spinner(14)
        self.busy.hide()
        self.count_label = QLabel("")
        bar.addPermanentWidget(self.busy)
        bar.addPermanentWidget(self.count_label)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy.setVisible(busy)
        if message:
            self.statusBar().showMessage(message, 0 if busy else 6000)
        elif not busy:
            self.statusBar().clearMessage()

    def _update_counts(self, selected: int | None = None) -> None:
        if self.photos.currentWidget() is not self.grid or self.grid.model.rowCount() == 0:
            self.count_label.setText("")
            self.export_action.setEnabled(False)
            return
        selected = self.grid.selected_count() if selected is None else selected
        text = self.grid.model.summary()
        if selected:
            text += f"  ·  {selected:,} selected"
        self.count_label.setText(text)
        self.export_action.setEnabled(selected > 0)

    # -- finding backups ---------------------------------------------------------------------------
    def scan_default_locations(self) -> None:
        self.central.setCurrentWidget(self.scanning_page)
        self._set_busy(True, "Looking for backups…")
        roots_fn, scan = self.services.default_roots, self.services.scan

        def work() -> tuple[list[BackupInfo], list[str]]:
            found: list[BackupInfo] = []
            problems: list[str] = []
            for root in roots_fn():
                try:
                    found.extend(scan(root))
                except Exception as exc:  # one unreadable root must not hide the other
                    problems.append(f"{root}: {error_text(exc)}")
            return found, problems

        self.runner.submit(work, on_done=self._defaults_scanned, on_error=self._defaults_failed)

    def _defaults_scanned(self, result: tuple[list[BackupInfo], list[str]]) -> None:
        found, problems = result
        for problem in problems:
            log.warning("scan problem: %s", problem)
        self.sidebar.set_backups(found)
        if not found:
            self._set_busy(False, "No backups in the usual places")
            self.central.setCurrentWidget(self.empty_page)
            return
        self._set_busy(False, f"Found {count(len(found), 'backup')}")
        self.central.setCurrentWidget(self.splitter)
        self._select_first_openable()

    def _defaults_failed(self, exc: BaseException) -> None:
        log.warning("could not scan the default backup folders", exc_info=exc)
        self._set_busy(False, "Couldn't look for backups")
        self.central.setCurrentWidget(self.empty_page)
        self.empty_page.set_note(f"Couldn't check the usual folders: {error_text(exc)}")

    def _select_first_openable(self) -> None:
        for row, info in enumerate(self.sidebar.model.backups()):
            if not info.is_encrypted and not is_incomplete(info):
                self.sidebar.select_row(row)
                return
        self.photos.setCurrentWidget(self.photo_hint)

    def _ask_directory(self, title: str) -> str:
        """Blocking folder picker. Tests replace this method."""
        return QFileDialog.getExistingDirectory(self, title)

    def open_folder(self) -> None:
        path = self._ask_directory("Open a backup folder, or a folder that contains backups")
        if path:
            self.scan_folder(Path(path))

    def scan_folder(self, path: Path) -> None:
        self._set_busy(True, f"Looking for backups in {path}…")
        self.runner.submit(
            self.services.scan,
            path,
            on_done=lambda found: self._folder_scanned(path, found),
            on_error=lambda exc: self._folder_failed(path, exc),
        )

    def _folder_scanned(self, path: Path, found: list[BackupInfo]) -> None:
        if not found:
            self._folder_failed(path, None)
            return
        rows = self.sidebar.add_backups(found)
        self._set_busy(False, f"Found {count(len(found), 'backup')} in {path}")
        self.central.setCurrentWidget(self.splitter)
        if rows:
            infos = self.sidebar.model.backups()
            openable = [r for r in rows if not infos[r].is_encrypted and not is_incomplete(infos[r])]
            self.sidebar.select_row((openable or rows)[0])

    def _folder_failed(self, path: Path, exc: BaseException | None) -> None:
        detail = f"No iPhone backups in {path}." if exc is None else f"Couldn't read {path}: {error_text(exc)}"
        self._set_busy(False, detail)
        if self.central.currentWidget() is not self.splitter:
            self.central.setCurrentWidget(self.empty_page)
            self.empty_page.set_note(detail)
        else:
            box = QMessageBox(QMessageBox.Icon.Information, "No backups found", detail, QMessageBox.StandardButton.Ok, self)
            box.setInformativeText("Pick the backup folder itself (it contains Manifest.db or Manifest.mbdb), or the folder above it.")
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            box.open()

    # -- opening a backup --------------------------------------------------------------------------
    def _backup_selected(self, info: BackupInfo) -> None:
        self._open_token += 1
        token = self._open_token
        if self.viewer is not None:
            self.viewer.close()
        self.grid.clear()
        self.backup, self.backup_info = None, info
        self._update_counts()
        name = info.device_name or "iPhone"
        if info.is_encrypted:
            self._show_photo_message("This backup is encrypted", ENCRYPTED_MESSAGE, lock=True)
            self.statusBar().showMessage(f"{name}: {ENCRYPTED_MESSAGE}", 8000)
            return
        if is_incomplete(info):
            self._show_photo_message(
                "This backup is incomplete",
                "There is no file list (Manifest) in it. The backup was probably interrupted or is still running.",
            )
            return
        self.photo_busy.show_message(f"Opening {name}…", "Reading the list of files. Large backups take a few seconds.")
        self.photos.setCurrentWidget(self.photo_busy)
        self._set_busy(True, f"Opening {name}…")
        open_backup, camera_roll = self.services.open_backup, self.services.camera_roll

        def work() -> tuple[Any, list]:
            backup = open_backup(info.path)
            return backup, camera_roll(backup)

        self.runner.submit(
            work,
            on_done=lambda result: self._backup_opened(token, info, result),
            on_error=lambda exc: self._backup_failed(token, info, exc),
        )

    def _show_photo_message(self, title: str, body: str, *, lock: bool = False) -> None:
        icon = icons.lock_pixmap(48, theme.muted_text(self.palette())) if lock else None
        self.photo_message.show_message(title, body, icon=icon)
        self.photos.setCurrentWidget(self.photo_message)
        self._update_counts()

    def _thumbnail_cache(self) -> ThumbnailSource | None:
        try:
            return self.services.thumbnail_cache()
        except Exception:
            log.warning("thumbnail cache unavailable; the grid will show placeholders", exc_info=True)
            return None

    def _cache_dir(self) -> Path:
        return Path(self.services.thumbnail_cache().cache_dir)

    def _backup_opened(self, token: int, info: BackupInfo, result: tuple[Any, list]) -> None:
        if token != self._open_token:
            return  # the user picked another backup meanwhile
        backup, items = result
        self.backup = backup
        name = info.device_name or "iPhone"
        self._set_busy(False, f"Opened {name}")
        if not items:
            self._show_photo_message(
                "No photos or videos",
                "This backup has no Camera Roll. If iCloud Photos was on, the originals live in iCloud, not in the backup.",
            )
            return
        self.grid.set_media(backup, items, self._thumbnail_cache())
        self.photos.setCurrentWidget(self.grid)
        self.tabs.setCurrentIndex(self.photos_tab)
        self._update_counts(0)

    def _backup_failed(self, token: int, info: BackupInfo, exc: BaseException) -> None:
        if token != self._open_token:
            return
        self._set_busy(False, "Couldn't open the backup")
        if isinstance(exc, EncryptedBackupError):
            self._show_photo_message("This backup is encrypted", ENCRYPTED_MESSAGE, lock=True)
            return
        log.warning("could not open %s", info.path, exc_info=exc)
        self._show_photo_message("Couldn't open this backup", error_text(exc))

    # -- viewer and export -------------------------------------------------------------------------
    def open_viewer(self, row: int) -> None:
        if self.viewer is None:
            self.viewer = MediaViewer(self.runner, self.services.open_image, self._cache_dir, parent=self)
        self.viewer.show_item(self.grid.model, row)

    def export_items(self, items: list) -> None:
        if self.backup is None or not items or self.export.running():
            return
        dest = self._ask_directory(f"Export {count(len(items), 'item')} to…")
        if dest:
            self.export.start(self.backup, items, Path(dest))

    # -- shutdown ----------------------------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop background work. Safe to call twice."""
        self._open_token += 1
        if self.export.running():
            self.export.cancel()
        if self.viewer is not None:
            self.viewer.close()
        self.grid.loader.shutdown()
        self.runner.shutdown()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)
