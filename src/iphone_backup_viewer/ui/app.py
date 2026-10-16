"""Application entry point."""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from .. import __version__
from . import icons, theme
from .main_window import MainWindow

APP_ID = "iPhoneBackupViewer"


def _windows_app_id() -> None:
    """Group the taskbar button under our icon instead of python.exe's."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_ID}.{APP_ID}")
        except Exception:  # cosmetic only
            pass


def run(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _windows_app_id()
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName(APP_ID)
    app.setOrganizationName(APP_ID)
    app.setApplicationVersion(__version__)
    theme.apply_theme(app)
    app.setWindowIcon(icons.app_icon())
    window = MainWindow()
    window.show()
    return app.exec()
