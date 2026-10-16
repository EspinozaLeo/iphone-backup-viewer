"""The sidebar: one row per backup found on disk."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from PySide6.QtCore import QAbstractListModel, QModelIndex, QPersistentModelIndex, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..core.models import BackupInfo
from . import icons, theme
from .formatting import short_datetime

InfoRole = Qt.ItemDataRole.UserRole + 1
IncompleteRole = Qt.ItemDataRole.UserRole + 2
EncryptedRole = Qt.ItemDataRole.UserRole + 3

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def is_incomplete(info: BackupInfo) -> bool:
    return info.manifest_format is None


def _path_key(info: BackupInfo) -> str:
    return os.path.normcase(os.path.abspath(info.path))


def subtitle(info: BackupInfo) -> str:
    if is_incomplete(info):
        return "Incomplete backup"
    parts = []
    if info.ios_version:
        parts.append(f"iOS {info.ios_version}")
    parts.append(short_datetime(info.last_backup) if info.last_backup else "Date unknown")
    return " · ".join(parts)


class BackupListModel(QAbstractListModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[BackupInfo] = []

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        info = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return info.device_name or "iPhone"
        if role == Qt.ItemDataRole.ToolTipRole:
            lines = [info.device_name or "iPhone"]
            if info.product_type:
                lines.append(info.product_type)
            if info.ios_version:
                lines.append(f"iOS {info.ios_version}")
            lines.append(f"Last backup: {short_datetime(info.last_backup)}")
            if info.is_encrypted:
                lines.append("Encrypted")
            if is_incomplete(info):
                lines.append("Incomplete: no manifest found (interrupted or still in progress)")
            lines.append(str(info.path))
            return "\n".join(lines)
        if role == InfoRole:
            return info
        if role == IncompleteRole:
            return is_incomplete(info)
        if role == EncryptedRole:
            return info.is_encrypted
        return None

    def info(self, row: int) -> BackupInfo:
        return self._items[row]

    def backups(self) -> list[BackupInfo]:
        return list(self._items)

    def set_backups(self, infos: list[BackupInfo]) -> None:
        self.beginResetModel()
        self._items = []
        seen: set[str] = set()
        for info in sorted(infos, key=lambda i: i.last_backup or _EPOCH, reverse=True):
            if _path_key(info) not in seen:
                seen.add(_path_key(info))
                self._items.append(info)
        self.endResetModel()

    def add_backups(self, infos: list[BackupInfo]) -> list[int]:
        """Merge in newly found backups (deduplicated by folder). Returns the rows of all of `infos`."""
        known = {_path_key(i) for i in self._items}
        fresh = [i for i in infos if _path_key(i) not in known]
        if fresh:
            self.set_backups(self._items + fresh)
        keys = {_path_key(i) for i in infos}
        return [row for row, i in enumerate(self._items) if _path_key(i) in keys]


class BackupDelegate(QStyledItemDelegate):
    ROW_HEIGHT = 58

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        return QSize(200, self.ROW_HEIGHT)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index) -> None:
        info: BackupInfo = index.data(InfoRole)
        pal = option.palette
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        incomplete = is_incomplete(info)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(option.rect).adjusted(6, 2, -6, -2)
        accent = pal.color(QPalette.ColorRole.Highlight)
        if selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            p.drawRoundedRect(card, 8, 8)
        elif hovered:
            hover = QColor(pal.color(QPalette.ColorRole.Text))
            hover.setAlphaF(0.06)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(hover)
            p.drawRoundedRect(card, 8, 8)

        if selected:
            title_c = QColor("white")
            sub_c = QColor(255, 255, 255, 200)
        elif incomplete:
            title_c = sub_c = pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        else:
            title_c = pal.color(QPalette.ColorRole.Text)
            sub_c = theme.muted_text(pal)

        badge = QRectF(card.left() + 8, card.center().y() - 17, 34, 34)
        if not selected:
            tint = QColor(accent if not incomplete else sub_c)
            tint.setAlphaF(0.14 if not incomplete else 0.10)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tint)
            p.drawRoundedRect(badge, 9, 9)
            glyph_c = accent if not incomplete else sub_c
        else:
            fill = QColor(255, 255, 255, 46)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(badge, 9, 9)
            glyph_c = QColor("white")
        icons.paint_phone(p, badge.adjusted(6, 6, -6, -6), glyph_c)

        right = card.right() - 10
        if info.is_encrypted:
            lock = QRectF(right - 16, card.center().y() - 8, 16, 16)
            icons.paint_lock(p, lock, title_c if selected else sub_c)
            right = lock.left() - 6

        text_left = badge.right() + 10
        width = max(10.0, right - text_left)
        tf = QFont(option.font)
        tf.setWeight(QFont.Weight.DemiBold)
        sf = QFont(option.font)
        sf.setPointSizeF(sf.pointSizeF() * 0.92)
        tm, sm = QFontMetrics(tf), QFontMetrics(sf)
        block = tm.height() + 2 + sm.height()
        y = card.center().y() - block / 2
        p.setFont(tf)
        p.setPen(title_c)
        name = tm.elidedText(info.device_name or "iPhone", Qt.TextElideMode.ElideRight, int(width))
        p.drawText(QPointF(text_left, y + tm.ascent()), name)
        p.setFont(sf)
        p.setPen(sub_c)
        sub = sm.elidedText(subtitle(info), Qt.TextElideMode.ElideRight, int(width))
        p.drawText(QPointF(text_left, y + tm.height() + 2 + sm.ascent()), sub)
        p.restore()


class BackupList(QWidget):
    """Sidebar widget. Emits `backupSelected` when the current row changes; the window decides
    what selecting means (open it, or explain why it can't be opened yet)."""

    backupSelected = Signal(object)  # BackupInfo
    openFolderRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Window)
        self.model = BackupListModel(self)
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(BackupDelegate(self.view))
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setMouseTracking(True)
        self.view.setUniformItemSizes(True)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.viewport().setAutoFillBackground(False)
        self.view.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.view.selectionModel().currentChanged.connect(self._current_changed)

        self.header = QLabel("BACKUPS")
        hf = QFont(self.font())
        hf.setPointSizeF(hf.pointSizeF() * 0.85)
        hf.setWeight(QFont.Weight.DemiBold)
        hf.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
        self.header.setFont(hf)
        self.count = QLabel("")
        self.count.setFont(hf)
        self.open_button = QPushButton("Open folder…")
        self.open_button.setToolTip("Open a backup folder, or a folder that contains backups")
        self.open_button.clicked.connect(self.openFolderRequested)

        top = QHBoxLayout()
        top.setContentsMargins(16, 14, 16, 6)
        top.addWidget(self.header)
        top.addStretch(1)
        top.addWidget(self.count)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(12, 8, 12, 12)
        bottom.addWidget(self.open_button)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addLayout(top)
        col.addWidget(self.view, 1)
        col.addLayout(bottom)
        self._built = True
        self._tint_header()

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == event.Type.PaletteChange and hasattr(self, "_built"):
            self._tint_header()
        super().changeEvent(event)

    def _tint_header(self) -> None:
        for label in (self.header, self.count):
            pal = label.palette()
            pal.setColor(QPalette.ColorRole.WindowText, theme.muted_text(self.palette()))
            label.setPalette(pal)

    def set_backups(self, infos: list[BackupInfo]) -> None:
        self.model.set_backups(infos)
        self._update_count()

    def add_backups(self, infos: list[BackupInfo]) -> list[int]:
        current = self.current()
        rows = self.model.add_backups(infos)
        if current is not None:  # keep the selection on the same backup after the re-sort
            for row, info in enumerate(self.model.backups()):
                if _path_key(info) == _path_key(current):
                    self.view.selectionModel().blockSignals(True)
                    self.view.setCurrentIndex(self.model.index(row))
                    self.view.selectionModel().blockSignals(False)
        self._update_count()
        return rows

    def _update_count(self) -> None:
        n = self.model.rowCount()
        self.count.setText(str(n) if n else "")

    def select_row(self, row: int) -> None:
        self.view.setCurrentIndex(self.model.index(row))

    def current(self) -> BackupInfo | None:
        idx = self.view.currentIndex()
        return self.model.info(idx.row()) if idx.isValid() else None

    def _current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if current.isValid():
            self.backupSelected.emit(self.model.info(current.row()))
