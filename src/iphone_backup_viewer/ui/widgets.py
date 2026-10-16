"""Small reusable widgets: a busy spinner, a centred message panel, a click-to-seek slider."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QSplitterHandle,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from . import theme


class Spinner(QWidget):
    """An indeterminate progress arc. Animates only while visible."""

    def __init__(self, size: int = 28, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(size, size)

    def _tick(self) -> None:
        self._angle = (self._angle + 6) % 360
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = min(self.width(), self.height())
        stroke = max(2.0, w / 9)
        r = QRectF(stroke, stroke, w - 2 * stroke, w - 2 * stroke)
        track = QColor(self.palette().color(QPalette.ColorRole.Text))
        track.setAlphaF(0.12)
        p.setPen(QPen(track, stroke))
        p.drawEllipse(r)
        pen = QPen(self.palette().color(QPalette.ColorRole.Highlight), stroke)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(r, int(-self._angle * 16), int(100 * 16))


class MessagePanel(QWidget):
    """Centred icon + title + body + optional button. Used for empty, busy and error states."""

    def __init__(self, parent: QWidget | None = None, *, busy: bool = False) -> None:
        super().__init__(parent)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spinner = Spinner(36) if busy else None
        self.title = QLabel()
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 1.45)
        f.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(f)
        self.body = QLabel()
        self.body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.button = QPushButton()
        self.button.setVisible(False)
        self.button.setMinimumWidth(150)
        self.button.setDefault(True)
        self.note = QLabel()
        self.note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.note.setWordWrap(True)
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setVisible(False)

        # Wrapped labels need the full column width (an alignment flag would shrink them to one
        # line), so they sit in a fixed-max-width column that is centred by stretches.
        column = QWidget()
        column.setMaximumWidth(480)
        column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        if self.spinner is not None:
            col.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignHCenter)
            col.addSpacing(16)
        col.addWidget(self.icon)
        col.addSpacing(10)
        col.addWidget(self.title)
        col.addSpacing(6)
        col.addWidget(self.body)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.button)
        row.addStretch(1)
        col.addSpacing(18)
        col.addLayout(row)
        col.addSpacing(10)
        col.addWidget(self.note)
        across = QHBoxLayout()
        across.setContentsMargins(24, 0, 24, 0)
        across.addStretch(1)
        across.addWidget(column, 10)
        across.addStretch(1)
        outer = QVBoxLayout(self)
        outer.addStretch(3)
        outer.addLayout(across)
        outer.addStretch(4)
        self._callback: Callable[[], None] | None = None
        self.button.clicked.connect(self._clicked)
        self._built = True
        self._apply_muted()

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == event.Type.PaletteChange and hasattr(self, "_built"):
            self._apply_muted()
        super().changeEvent(event)

    def _apply_muted(self) -> None:
        for label in (self.body, self.note):
            pal = label.palette()
            pal.setColor(QPalette.ColorRole.WindowText, theme.muted_text(self.palette()))
            label.setPalette(pal)

    def _clicked(self) -> None:
        if self._callback is not None:
            self._callback()

    def show_message(
        self,
        title: str,
        body: str = "",
        *,
        icon: QPixmap | None = None,
        button: str | None = None,
        on_click: Callable[[], None] | None = None,
        note: str = "",
        rich: bool = False,
    ) -> None:
        """`body` is plain text unless `rich` (messages often embed paths and exception text)."""
        self.title.setText(title)
        self.body.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
        self.body.setText(body)
        self.body.setVisible(bool(body))
        self.icon.setVisible(icon is not None)
        if icon is not None:
            self.icon.setPixmap(icon)
        self.button.setVisible(button is not None)
        self.button.setText(button or "")
        self._callback = on_click
        self.set_note(note)

    def set_note(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))


class ClickSlider(QSlider):
    """A horizontal slider that jumps to where you click instead of paging towards it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, self
            )
            if not handle.contains(event.position().toPoint()):
                groove = self.style().subControlRect(
                    QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderGroove, self
                )
                span = max(1, groove.width() - handle.width())
                x = int(event.position().x()) - groove.left() - handle.width() // 2
                value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span)
                self.setValue(value)
                self.sliderMoved.emit(value)
        super().mousePressEvent(event)

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(240, super().sizeHint().height())


class _HairlineHandle(QSplitterHandle):
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        pal = self.palette()
        p.fillRect(self.rect(), theme.mix(pal.color(QPalette.ColorRole.Window), pal.color(QPalette.ColorRole.Text), 0.13))


class HairlineSplitter(QSplitter):
    """A splitter whose handle is a 1px divider line in the palette's colours."""

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self.setHandleWidth(1)

    def createHandle(self) -> QSplitterHandle:  # noqa: N802
        return _HairlineHandle(self.orientation(), self)
