"""Full-size viewer: photos decoded off the GUI thread, videos and Live Photo motion via Qt Multimedia."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image
from PySide6.QtCore import QPersistentModelIndex, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QKeyEvent, QPainter, QPalette, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME
from ..core.errors import MissingFileError
from ..core.models import FileEntry, MediaItem, MediaKind
from . import icons, theme
from .formatting import duration_text, error_text, long_datetime
from .playback import playable_alias
from .widgets import ClickSlider, Spinner
from .workers import TaskRunner

log = logging.getLogger(__name__)

DECODED_CACHE = 5  # decoded stills kept for instant Left/Right (current, neighbours, one spare)
_CONTROL_INK = QColor(236, 236, 241)


def pil_to_qimage(img: Image.Image) -> QImage:
    """Convert a decoded PIL image into a QImage that owns its pixels. Safe on any thread."""
    if img.mode not in ("RGB", "RGBA"):
        has_alpha = "A" in img.getbands() or "transparency" in img.info
        img = img.convert("RGBA" if has_alpha else "RGB")
    if img.mode == "RGB":
        fmt, bpl = QImage.Format.Format_RGB888, img.width * 3
    else:
        fmt, bpl = QImage.Format.Format_RGBA8888, img.width * 4
    data = img.tobytes("raw", img.mode)
    return QImage(data, img.width, img.height, bpl, fmt).copy()


def decode_still(backup: Any, entry: FileEntry, open_image: Callable[..., Image.Image], max_size: int) -> QImage:
    """Worker body: local copy -> PIL decode (EXIF-rotated, downscaled) -> QImage."""
    path = backup.local_path(entry)
    with open_image(path, max_size=max_size) as img:
        return pil_to_qimage(img)


class ImageCanvas(QWidget):
    """Draws one image scaled to fit (never past 1 image pixel per device pixel), or a status line."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._image: QImage | None = None
        self._pixmap: QPixmap | None = None
        self._scaled: QPixmap | None = None
        self._scaled_key: tuple | None = None
        self._message = ""
        self._detail = ""
        self.spinner = Spinner(34, self)
        self.spinner.hide()

    def image(self) -> QImage | None:
        return self._image

    def message(self) -> str:
        return self._message

    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self._pixmap = QPixmap.fromImage(image) if image is not None else None
        self._scaled = None
        self._scaled_key = None
        self._message = self._detail = ""
        self.spinner.hide()
        self.update()

    def set_loading(self) -> None:
        self.set_image(None)
        self._place_spinner()
        self.spinner.show()

    def set_message(self, message: str, detail: str = "") -> None:
        self.set_image(None)
        self._message, self._detail = message, detail
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._place_spinner()
        super().resizeEvent(event)

    def _place_spinner(self) -> None:
        self.spinner.move(self.width() // 2 - self.spinner.width() // 2, self.height() // 2 - self.spinner.height() // 2)

    def fitted_size(self) -> QSize:
        """Logical size the image is drawn at for the current widget size."""
        if self._pixmap is None:
            return QSize()
        dpr = self.devicePixelRatioF() or 1.0
        img = QSize(self._pixmap.width(), self._pixmap.height())
        box = QSize(round(self.width() * dpr), round(self.height() * dpr))
        device = img.scaled(box, Qt.AspectRatioMode.KeepAspectRatio) if (img.width() > box.width() or img.height() > box.height()) else img
        return QSize(max(1, round(device.width() / dpr)), max(1, round(device.height() / dpr)))

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Window))
        if self._pixmap is not None:
            dpr = self.devicePixelRatioF() or 1.0
            logical = self.fitted_size()
            key = (logical.width(), logical.height(), dpr)
            if self._scaled_key != key:
                device = QSize(round(logical.width() * dpr), round(logical.height() * dpr))
                if device == self._pixmap.size():
                    self._scaled = QPixmap(self._pixmap)
                else:
                    self._scaled = self._pixmap.scaled(device, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self._scaled.setDevicePixelRatio(dpr)
                self._scaled_key = key
            x = (self.width() - logical.width()) / 2
            y = (self.height() - logical.height()) / 2
            p.drawPixmap(QPointF(x, y), self._scaled)
        elif self._message:
            p.setPen(QColor(236, 236, 241))
            f = QFont(self.font())
            f.setPointSizeF(f.pointSizeF() * 1.25)
            f.setWeight(QFont.Weight.DemiBold)
            p.setFont(f)
            mid = self.height() / 2
            p.drawText(QRectF(0, mid - 40, self.width(), 30), Qt.AlignmentFlag.AlignCenter, self._message)
            if self._detail:
                p.setPen(QColor(160, 160, 168))
                p.setFont(self.font())
                p.drawText(QRectF(20, mid - 4, self.width() - 40, 60), Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap, self._detail)


class MediaViewer(QWidget):
    """One top-level window, reused. Walks the grid model's current order, so sorting carries over."""

    closed = Signal()

    def __init__(
        self,
        runner: TaskRunner,
        open_image: Callable[..., Image.Image],
        cache_dir: Callable[[], Path],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self._runner = runner
        self._open_image = open_image
        self._cache_dir = cache_dir
        self._model = None
        self._current = QPersistentModelIndex()
        self._token = 0
        self._decoded: OrderedDict[str, QImage] = OrderedDict()
        self._inflight: set[str] = set()
        self._player: QMediaPlayer | None = None
        self._audio: QAudioOutput | None = None
        self._video: QVideoWidget | None = None
        self._playing: FileEntry | None = None
        self._playing_live = False
        self._play_path: Path | None = None
        self._alias_tried = False
        self._sized = False

        self.setPalette(theme.viewer_palette())
        self.setAutoFillBackground(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(560, 400)

        # Top bar
        self.prev_button = self._tool("chevron-left", "Previous (Left arrow)", self.previous)
        self.next_button = self._tool("chevron-right", "Next (Right arrow)", self.next)
        self.title = QLabel()
        tf = QFont(self.font())
        tf.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(tf)
        self.date = QLabel()
        self.date.setStyleSheet("color: #a0a0a8;")
        self.position = QLabel()
        self.position.setStyleSheet("color: #a0a0a8;")
        self.live_button = QPushButton("Play Live")
        self.live_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.live_button.setToolTip("Play this Live Photo's motion")
        self.live_button.clicked.connect(self.play_live)
        self.live_button.setIcon(self._live_icon())
        top = QHBoxLayout()
        top.setContentsMargins(10, 8, 14, 8)
        top.setSpacing(6)
        top.addWidget(self.prev_button)
        top.addWidget(self.next_button)
        top.addSpacing(8)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(self.title)
        titles.addWidget(self.date)
        top.addLayout(titles)
        top.addStretch(1)
        top.addWidget(self.live_button)
        top.addSpacing(10)
        top.addWidget(self.position)

        # Centre
        self.canvas = ImageCanvas()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.canvas)

        # Video controls
        self.play_button = self._tool("play", "Play / pause (Space)", self.toggle_play)
        self.elapsed = QLabel("0:00")
        self.seek = ClickSlider()
        self.seek.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.seek.sliderMoved.connect(self._seek)
        self.total = QLabel("0:00")
        self.mute_button = self._tool("volume", "Mute", self.toggle_mute)
        for label in (self.elapsed, self.total):
            label.setStyleSheet("color: #c8c8cf;")
            label.setMinimumWidth(42)
        self.total.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar = QHBoxLayout()
        bar.setContentsMargins(12, 6, 12, 10)
        bar.setSpacing(8)
        bar.addWidget(self.play_button)
        bar.addWidget(self.elapsed)
        bar.addWidget(self.seek, 1)
        bar.addWidget(self.total)
        bar.addWidget(self.mute_button)
        self.controls = QWidget()
        self.controls.setLayout(bar)
        self.controls.hide()

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addLayout(top)
        col.addWidget(self.stack, 1)
        col.addWidget(self.controls)

    def _tool(self, glyph: str, tip: str, slot: Callable[[], None]) -> QToolButton:
        b = QToolButton()
        b.setIcon(icons.glyph_icon(glyph, _CONTROL_INK))
        b.setIconSize(QSize(22, 22))
        b.setAutoRaise(True)
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(slot)
        return b

    @staticmethod
    def _live_icon() -> QPixmap:
        dpr = QGuiApplication.instance().devicePixelRatio()
        pm = QPixmap(round(16 * dpr), round(16 * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        icons.paint_live(p, QPointF(8, 8), 7, QColor(236, 236, 241))
        p.end()
        return pm

    # -- public API --------------------------------------------------------------------------------
    def show_item(self, model, row: int) -> None:
        if model is not self._model:
            if self._model is not None:
                self._model.modelReset.disconnect(self.close)
            self._model = model
            self._decoded.clear()
            self._inflight.clear()
            model.modelReset.connect(self.close)
        self._current = QPersistentModelIndex(model.index(row))
        if not self._sized:
            self._initial_geometry()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._show_current()

    def current_row(self) -> int:
        return self._current.row() if self._current.isValid() else -1

    def current_item(self) -> MediaItem | None:
        if self._model is None or not self._current.isValid():
            return None
        return self._model.item(self._current.row())

    def next(self) -> None:
        self._step(+1)

    def previous(self) -> None:
        self._step(-1)

    def _step(self, delta: int) -> None:
        row = self.current_row()
        if self._model is None or row < 0:
            return
        target = row + delta
        if 0 <= target < self._model.rowCount():
            self._current = QPersistentModelIndex(self._model.index(target))
            self._show_current()

    # -- keyboard ----------------------------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Right:
            self.next()
        elif key == Qt.Key.Key_Left:
            self.previous()
        elif key == Qt.Key.Key_Escape:
            self.close()
        elif key == Qt.Key.Key_Space:
            item = self.current_item()
            if item is not None and item.kind == MediaKind.VIDEO:
                self.toggle_play()
            elif item is not None and item.live_video is not None:
                self.play_live()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_video()
        self._token += 1
        self.closed.emit()
        super().closeEvent(event)

    # -- showing an item ---------------------------------------------------------------------------
    def _initial_geometry(self) -> None:
        self._sized = True
        screen = (self.parentWidget().screen() if self.parentWidget() else None) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        w, h = int(area.width() * 0.78), int(area.height() * 0.82)
        self.resize(w, h)
        self.move(area.left() + (area.width() - w) // 2, area.top() + (area.height() - h) // 2)

    def _max_decode_size(self) -> int:
        screen = self.screen() or QGuiApplication.primaryScreen()
        size = screen.geometry().size()
        return max(1024, round(max(size.width(), size.height()) * screen.devicePixelRatio()))

    def _show_current(self) -> None:
        item = self.current_item()
        if item is None:
            self.close()
            return
        self._token += 1
        self._stop_video()
        row, n = self.current_row(), self._model.rowCount()
        self.title.setText(item.entry.name)
        self.date.setText(long_datetime(item.taken))
        self.position.setText(f"{row + 1:,} of {n:,}")
        self.setWindowTitle(f"{item.entry.name} — {APP_NAME}")
        self.prev_button.setEnabled(row > 0)
        self.next_button.setEnabled(row < n - 1)
        self.live_button.setVisible(item.live_video is not None)
        self.live_button.setEnabled(True)
        self.live_button.setToolTip("Play this Live Photo's motion (Space)")
        if item.kind == MediaKind.VIDEO:
            self._play(item.entry, live=False)
        else:
            self.controls.hide()
            self.stack.setCurrentWidget(self.canvas)
            cached = self._decoded.get(item.entry.file_id)
            if cached is not None:
                self._decoded.move_to_end(item.entry.file_id)
                self.canvas.set_image(cached)
            else:
                self.canvas.set_loading()
                self._decode(item.entry)
        self._prefetch_neighbours()

    def _decode(self, entry: FileEntry) -> None:
        key = entry.file_id
        if key in self._inflight or key in self._decoded or self._model is None:
            return
        self._inflight.add(key)
        backup = self._model.backup
        self._runner.submit(
            decode_still,
            backup,
            entry,
            self._open_image,
            self._max_decode_size(),
            on_done=lambda image, key=key: self._decoded_ready(key, image),
            on_error=lambda exc, key=key: self._decode_failed(key, exc),
        )

    def _decoded_ready(self, key: str, image: QImage) -> None:
        self._inflight.discard(key)
        self._decoded[key] = image
        self._decoded.move_to_end(key)
        while len(self._decoded) > DECODED_CACHE:
            self._decoded.popitem(last=False)
        item = self.current_item()
        if item is not None and item.entry.file_id == key and self.stack.currentWidget() is self.canvas and not self._playing_live:
            self.canvas.set_image(image)

    def _decode_failed(self, key: str, exc: BaseException) -> None:
        self._inflight.discard(key)
        item = self.current_item()
        if item is None or item.entry.file_id != key:
            return
        if isinstance(exc, MissingFileError):
            self.canvas.set_message("This photo isn't in the backup", "The manifest lists it, but its data was never copied.")
        else:
            log.warning("could not decode %s: %s", item.entry.relative_path, exc)
            self.canvas.set_message("Can't show this photo", error_text(exc))

    def _prefetch_neighbours(self) -> None:
        row = self.current_row()
        if self._model is None or row < 0:
            return
        for r in (row + 1, row - 1):
            if 0 <= r < self._model.rowCount():
                neighbour = self._model.item(r)
                if neighbour.kind == MediaKind.PHOTO:
                    self._decode(neighbour.entry)

    # -- video and Live Photo motion ---------------------------------------------------------------
    def _ensure_player(self) -> None:
        if self._player is not None:
            return
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._video = QVideoWidget()
        self._video.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._video.setAutoFillBackground(True)
        self.stack.addWidget(self._video)
        self._player.setVideoOutput(self._video)
        self._player.positionChanged.connect(self._position_changed)
        self._player.durationChanged.connect(self._duration_changed)
        self._player.playbackStateChanged.connect(self._state_changed)
        self._player.mediaStatusChanged.connect(self._status_changed)
        self._player.errorOccurred.connect(self._player_error)

    @property
    def player(self) -> QMediaPlayer | None:
        return self._player

    def play_live(self) -> None:
        item = self.current_item()
        if item is None or item.live_video is None:
            return
        if self._playing_live:  # pressing again replays from the start
            self._player.setPosition(0)
            self._player.play()
            return
        self._play(item.live_video, live=True)

    def _play(self, entry: FileEntry, *, live: bool) -> None:
        self._ensure_player()
        token = self._token
        self._playing = entry
        self._playing_live = live
        self._alias_tried = False
        self.controls.setVisible(not live)
        self.live_button.setEnabled(not live)
        if not live:
            self.canvas.set_loading()
            self.stack.setCurrentWidget(self.canvas)
        backup = self._model.backup
        self._runner.submit(
            backup.local_path,
            entry,
            on_done=lambda path: self._start_source(token, entry, Path(path)),
            on_error=lambda exc: self._video_failed(token, exc),
        )

    def _start_source(self, token: int, entry: FileEntry, path: Path) -> None:
        if token != self._token or self._playing is not entry:
            return
        self._play_path = path
        self.stack.setCurrentWidget(self._video)
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()

    def _player_error(self, error: QMediaPlayer.Error, message: str) -> None:
        entry, token = self._playing, self._token
        if entry is None or error == QMediaPlayer.Error.NoError:
            return
        if not self._alias_tried and self._play_path is not None and error in (
            QMediaPlayer.Error.ResourceError,
            QMediaPlayer.Error.FormatError,
        ):
            # The backend may want an extension to pick a demuxer. Retry once with one.
            self._alias_tried = True
            log.info("retrying %s through an extension alias (%s)", entry.relative_path, message)
            backup = self._model.backup
            source = self._play_path
            self._runner.submit(
                lambda: playable_alias(source, self._cache_dir(), backup.info.udid, entry.file_id, entry.suffix or ".mov"),
                on_done=lambda path: self._start_source(token, entry, Path(path)),
                on_error=lambda exc: self._video_failed(token, exc),
            )
            return
        self._video_failed(token, RuntimeError(message or error.name))

    def _video_failed(self, token: int, exc: BaseException) -> None:
        if token != self._token:
            return
        live = self._playing_live
        self._stop_video()
        self.stack.setCurrentWidget(self.canvas)
        if live:
            self.live_button.setEnabled(False)
            self.live_button.setToolTip(f"The motion part could not be played: {error_text(exc)}")
            return
        if isinstance(exc, MissingFileError):
            self.canvas.set_message("This video isn't in the backup", "The manifest lists it, but its data was never copied.")
        else:
            self.canvas.set_message("Can't play this video", error_text(exc))

    def _stop_video(self) -> None:
        live_was_playing = self._playing_live
        self._playing = None
        self._playing_live = False
        self._play_path = None
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())  # release the file handle
        self.seek.setRange(0, 0)
        self.elapsed.setText("0:00")
        self.total.setText("0:00")
        if live_was_playing:
            self.stack.setCurrentWidget(self.canvas)
            self.live_button.setEnabled(True)

    def toggle_play(self) -> None:
        if self._player is None or self._playing is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def toggle_mute(self) -> None:
        if self._audio is None:
            return
        self._audio.setMuted(not self._audio.isMuted())
        self.mute_button.setIcon(icons.glyph_icon("mute" if self._audio.isMuted() else "volume", _CONTROL_INK))
        self.mute_button.setToolTip("Unmute" if self._audio.isMuted() else "Mute")

    def _seek(self, ms: int) -> None:
        if self._player is not None:
            self._player.setPosition(ms)

    def _position_changed(self, ms: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.setValue(ms)
        self.elapsed.setText(duration_text(ms))

    def _duration_changed(self, ms: int) -> None:
        self.seek.setRange(0, max(0, ms))
        self.seek.setPageStep(max(1000, ms // 20))
        self.total.setText(duration_text(ms))

    def _state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setIcon(icons.glyph_icon("pause" if playing else "play", _CONTROL_INK))

    def _status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self._playing_live:
            token = self._token
            # Back to the still, like on the phone. Deferred: don't reset the player from inside its own signal.
            QTimer.singleShot(0, lambda: self._stop_video() if token == self._token and self._playing_live else None)
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._player.setPosition(0)
            self._player.pause()
