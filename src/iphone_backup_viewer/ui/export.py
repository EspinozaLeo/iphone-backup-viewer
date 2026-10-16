"""Copy selected originals out of the backup into a folder the user picks.

`export_items` is plain Python and runs on a worker; `ExportController` wraps it in a cancellable
progress dialog and reports the outcome.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QWidget

from ..core.errors import EncryptedBackupError, MissingFileError
from ..core.models import FileEntry, MediaItem
from .formatting import count, error_text
from .workers import TaskRunner, safe_emit

_CHUNK = 1 << 20
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


class _Cancelled(Exception):
    pass


@dataclass
class ExportResult:
    dest: Path
    total: int = 0  # files attempted, Live Photo motion included
    copied: list[Path] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (file name, reason)
    cancelled: bool = False


def safe_filename(name: str, fallback: str) -> str:
    """A name that is valid on Windows and cannot leave the export folder. Names come from the
    backup's manifest, so treat them as untrusted (a backslash in an iOS name is a path on Windows)."""
    cleaned = _BAD_CHARS.sub("_", name).strip().rstrip(". ")
    if not cleaned or set(cleaned) <= {"."}:
        cleaned = fallback
    stem, ext = os.path.splitext(cleaned)
    if stem.split(".")[0].upper() in _RESERVED:
        stem = "_" + stem
    if len(stem) > 150:
        stem = stem[:150]
    return stem + ext[:20]


def _candidate(dest: Path, name: str, n: int) -> Path:
    if n == 0:
        return dest / name
    stem, ext = os.path.splitext(name)
    return dest / f"{stem} ({n}){ext}"


def reserve_names(dest: Path, names: list[str]) -> list[Path]:
    """Claim free paths for a group of files that belong together, using the same collision suffix for
    all of them, so a Live Photo stays a pair: IMG_0001 (1).HEIC + IMG_0001 (1).MOV. Files are created
    empty with O_EXCL, so two exports into one folder can never pick the same name."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    n = 0
    while True:
        paths = [_candidate(dest, name, n) for name in names]
        if len({p.name.lower() for p in paths}) == len(paths):
            created: list[Path] = []
            try:
                for p in paths:
                    os.close(os.open(p, flags))
                    created.append(p)
                return paths
            except OSError as exc:
                for p in created:
                    p.unlink(missing_ok=True)
                # Taken (by a file, or a folder of that name): try the next suffix. Anything else
                # (read-only folder, disk full) is a real error for the caller.
                if not isinstance(exc, FileExistsError) and not os.path.lexists(p):
                    raise
        n += 1


def _copy(src: Path, dst: Path, cancelled: Callable[[], bool]) -> None:
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            if cancelled():
                raise _Cancelled
            chunk = fin.read(_CHUNK)
            if not chunk:
                break
            fout.write(chunk)


def _set_mtime(path: Path, modified: datetime | None) -> None:
    if modified is not None:
        ts = modified.timestamp()
        os.utime(path, (ts, ts))


def export_files(item: MediaItem) -> list[FileEntry]:
    """The files one item exports as: the original, plus the Live Photo motion clip if there is one."""
    return [item.entry] + ([item.live_video] if item.live_video is not None else [])


def export_items(
    backup: Any,
    items: list[MediaItem],
    dest: Path,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> ExportResult:
    """Copy each item's original (and Live Photo .MOV) into `dest`, keeping file names and modified times.
    Missing files are skipped and reported. Blocking: run it on a worker."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    groups = [export_files(item) for item in items]
    result = ExportResult(dest, total=sum(len(g) for g in groups))
    done = 0

    def tick(name: str) -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, result.total, name)

    for group in groups:
        if cancelled():
            result.cancelled = True
            break
        sources: list[tuple[FileEntry, Path]] = []
        for entry in group:
            try:
                sources.append((entry, Path(backup.local_path(entry))))
            except MissingFileError:
                result.skipped.append((entry.name, "not in this backup"))
                tick(entry.name)
            except EncryptedBackupError:
                result.skipped.append((entry.name, "encrypted"))
                tick(entry.name)
            except Exception as exc:  # one unreadable file must not stop the rest
                result.skipped.append((entry.name, error_text(exc)))
                tick(entry.name)
        if not sources:
            continue
        targets = reserve_names(dest, [safe_filename(e.name, e.file_id) for e, _ in sources])
        pending = list(targets)
        try:
            for (entry, src), target in zip(sources, targets):
                try:
                    _copy(src, target, cancelled)
                    _set_mtime(target, entry.modified)
                except OSError as exc:
                    target.unlink(missing_ok=True)
                    result.skipped.append((entry.name, f"copy failed: {exc.strerror or exc}"))
                else:
                    result.copied.append(target)
                pending.remove(target)
                tick(entry.name)
        except _Cancelled:
            result.cancelled = True
            break
        finally:
            for leftover in pending:  # reserved but not (fully) written
                leftover.unlink(missing_ok=True)
    return result


class ExportController(QObject):
    """Runs one export at a time behind a cancellable progress dialog, then reports what happened."""

    finished = Signal(object)  # ExportResult
    _progress = Signal(int, int, str)

    def __init__(self, parent: QWidget, runner: TaskRunner) -> None:
        super().__init__(parent)
        self._parent = parent
        self._runner = runner
        self._cancel = threading.Event()
        self.dialog: QProgressDialog | None = None
        self.report: QMessageBox | None = None
        self._progress.connect(self._on_progress)

    def running(self) -> bool:
        return self.dialog is not None

    def start(self, backup: Any, items: list[MediaItem], dest: Path) -> bool:
        if self.running() or not items:
            return False
        self._cancel = threading.Event()
        total = sum(len(export_files(i)) for i in items)
        dlg = QProgressDialog(f"Exporting {count(total, 'file')}…", "Cancel", 0, total, self._parent)
        dlg.setWindowTitle("Export")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(300)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)
        dlg.canceled.connect(self.cancel)
        self.dialog = dlg
        signal = self._progress
        self._runner.submit(
            export_items,
            backup,
            items,
            Path(dest),
            progress=lambda d, t, name: safe_emit(signal, d, t, name),
            cancelled=self._cancel.is_set,
            on_done=self._done,
            on_error=self._failed,
        )
        return True

    def cancel(self) -> None:
        self._cancel.set()
        if self.dialog is not None:
            self.dialog.setLabelText("Cancelling…")

    def _on_progress(self, done: int, total: int, name: str) -> None:
        if self.dialog is not None and not self._cancel.is_set():
            self.dialog.setMaximum(total)
            self.dialog.setValue(done)
            self.dialog.setLabelText(f"Exporting {done:,} of {total:,}\n{name}")

    def _close_dialog(self) -> None:
        if self.dialog is not None:
            self.dialog.canceled.disconnect(self.cancel)
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None

    def _done(self, result: ExportResult) -> None:
        self._close_dialog()
        self._show_report(result)
        self.finished.emit(result)

    def _failed(self, exc: BaseException) -> None:
        self._close_dialog()
        box = QMessageBox(QMessageBox.Icon.Warning, "Export failed", f"Nothing more could be exported.\n\n{error_text(exc)}", QMessageBox.StandardButton.Ok, self._parent)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.report = box
        box.open()
        self.finished.emit(None)

    def _show_report(self, result: ExportResult) -> None:
        copied = len(result.copied)
        if result.cancelled:
            title, text = "Export cancelled", f"Exported {count(copied, 'file')} before you cancelled."
        else:
            title, text = "Export complete", f"Exported {count(copied, 'file')} to {result.dest}."
        icon = QMessageBox.Icon.Information
        if result.skipped:
            icon = QMessageBox.Icon.Warning
            names = ", ".join(name for name, _ in result.skipped[:5])
            more = f" and {len(result.skipped) - 5:,} more" if len(result.skipped) > 5 else ""
            text += f"\n\nSkipped {count(len(result.skipped), 'file')} that could not be copied: {names}{more}."
        box = QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, self._parent)
        if result.skipped:
            box.setDetailedText("\n".join(f"{name}: {reason}" for name, reason in result.skipped))
        if copied:
            show = box.addButton("Show folder", QMessageBox.ButtonRole.ActionRole)
            show.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.dest))))
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.report = box
        box.open()
