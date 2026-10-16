"""The camera-roll grid: a QListView in IconMode over MediaItems, with lazily loaded thumbnails.

Thumbnail flow (see docs/ARCHITECTURE.md):
    model.data(DecorationRole) -> cached QPixmap, or a placeholder + ThumbnailLoader.request()
    request() -> one QRunnable per thumbnail on a dedicated QThreadPool -> ThumbnailCache.get()
    worker: JPEG on disk -> QImage, square-cropped to the tile size (QImage is safe off the GUI thread)
    signal (queued) -> GUI thread: QPixmap.fromImage -> bounded LRU -> dataChanged -> repaint
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Iterable
from enum import IntEnum
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QEasingCurve,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThread,
    QThreadPool,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.errors import MissingFileError
from ..core.models import FileEntry, MediaItem, MediaKind
from . import icons, theme
from .formatting import count, long_datetime, month_year, year
from .services import ThumbnailSource
from .workers import safe_emit

log = logging.getLogger(__name__)

ItemRole = Qt.ItemDataRole.UserRole + 1
StateRole = Qt.ItemDataRole.UserRole + 2

MIN_TILE = 150  # logical px; columns = viewport width // MIN_TILE, then tiles stretch to fill the row
GAP = 2  # logical px between tiles (half on each side)


class ThumbState(IntEnum):
    NONE = 0  # never requested, or evicted from the LRU
    LOADING = 1
    READY = 2
    MISSING = 3  # listed in the manifest, but its data is not in the backup
    BROKEN = 4  # present but undecodable


class PixmapLRU:
    """QPixmaps by key, evicting least-recently-used past a byte budget. GUI thread only."""

    def __init__(self, budget_bytes: int) -> None:
        self.budget = budget_bytes
        self.used = 0
        self._items: OrderedDict[str, tuple[QPixmap, int]] = OrderedDict()

    @staticmethod
    def _cost(pm: QPixmap) -> int:
        return max(1, pm.width() * pm.height() * 4)

    def get(self, key: str) -> QPixmap | None:
        hit = self._items.get(key)
        if hit is None:
            return None
        self._items.move_to_end(key)
        return hit[0]

    def __contains__(self, key: str) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    def put(self, key: str, pm: QPixmap) -> None:
        if key in self._items:
            self.used -= self._items.pop(key)[1]
        cost = self._cost(pm)
        self._items[key] = (pm, cost)
        self.used += cost
        while self.used > self.budget and len(self._items) > 1:
            _, (_, c) = self._items.popitem(last=False)
            self.used -= c

    def clear(self) -> None:
        self._items.clear()
        self.used = 0


def square_crop(image: QImage, px: int) -> QImage:
    """Centre-crop to a square and scale to `px` (aspect fill, like the iOS grid). Any thread."""
    side = min(image.width(), image.height())
    x = (image.width() - side) // 2
    y = (image.height() - side) // 2
    square = image.copy(x, y, side, side)
    if side != px:
        square = square.scaled(px, px, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return square


def _render_thumbnail(cache: ThumbnailSource, backup: Any, entry: FileEntry, size: int, px: int) -> tuple[QImage | None, ThumbState]:
    try:
        path = cache.get(backup, entry, size)
    except Exception:  # ThumbnailCache promises not to raise; one bad file still must not break the grid
        log.warning("thumbnail failed for %s", entry.relative_path, exc_info=True)
        return None, ThumbState.BROKEN
    if path is None:
        # get() returns None for both missing and undecodable files. Ask which, so a file that was
        # never backed up looks different from a corrupt one. Only on the failure path, so it is rare.
        try:
            backup.local_path(entry)
        except MissingFileError:
            return None, ThumbState.MISSING
        except Exception:
            pass
        return None, ThumbState.BROKEN
    image = QImage(str(path))
    if image.isNull():
        return None, ThumbState.BROKEN
    return square_crop(image, px), ThumbState.READY


class _ThumbJob(QRunnable):
    def __init__(self, loader: ThumbnailLoader, gen: int, key: str, entry: FileEntry, size: int, px: int) -> None:
        super().__init__()
        self._loader = loader
        self._lock = loader._lock
        self._backup = loader._backup
        self._cache = loader._cache
        self.gen, self.key, self.entry, self.size, self.px = gen, key, entry, size, px
        self.started = False
        self.cancelled = False

    def run(self) -> None:
        with self._lock:
            if self.cancelled:
                return
            self.started = True
        image, state = _render_thumbnail(self._cache, self._backup, self.entry, self.size, self.px)
        safe_emit(self._loader._finished, self.gen, self.key, image, int(state))


class ThumbnailLoader(QObject):
    """Owns the thumbnail worker pool, the in-flight jobs and the pixmap LRU for one open backup.

    Never enqueues a key twice. Newer requests run first: a paint pass's requests share a priority
    "epoch" above every older pass, in paint order, so the screen you are looking at fills top-left
    first, and rows you scrolled past wait (or are pruned). Prefetch requests run below all of them.
    """

    thumbnailReady = Signal(str)
    _finished = Signal(int, str, object, int)  # emitted by workers; delivered queued on the GUI thread

    EPOCH_SPAN = 10_000
    MAX_EPOCH = 200_000  # keeps priorities inside a C int

    def __init__(self, parent: QObject | None = None, *, budget_bytes: int = 192 * 1024 * 1024, threads: int | None = None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        # Few threads on purpose: workers run Python (and hold the GIL between C calls), so past ~3 they
        # mostly steal time from painting. Measured with 20k items: 6 threads made a full-screen repaint
        # ~38 ms while thumbnails generated, 3 threads ~21 ms, 1 thread ~16 ms.
        self.pool.setMaxThreadCount(threads or max(2, min(3, QThread.idealThreadCount() - 1)))
        self._lock = threading.Lock()
        self._jobs: dict[str, _ThumbJob] = {}
        self._failed: dict[str, ThumbState] = {}
        self.lru = PixmapLRU(budget_bytes)
        self._gen = 0
        self._backup: Any = None
        self._cache: ThumbnailSource | None = None
        self.tile_px = MIN_TILE
        self._epoch = 0
        self._seq = 0
        self._epoch_open = False
        self._finished.connect(self._on_finished)

    # -- lifecycle ---------------------------------------------------------------------------------
    def reset(self, backup: Any, cache: ThumbnailSource | None) -> None:
        self._gen += 1
        self._cancel_queued(keep=())
        self._jobs.clear()
        self._failed.clear()
        self.lru.clear()
        self._backup, self._cache = backup, cache
        self._epoch = 0

    def shutdown(self, timeout_ms: int = 5000) -> None:
        self.reset(None, None)
        self.pool.clear()
        self.pool.waitForDone(timeout_ms)

    # -- queries -----------------------------------------------------------------------------------
    def state(self, key: str) -> ThumbState:
        if key in self.lru:
            return ThumbState.READY
        if key in self._failed:
            return self._failed[key]
        if key in self._jobs:
            return ThumbState.LOADING
        return ThumbState.NONE

    def pixmap(self, key: str) -> QPixmap | None:
        return self.lru.get(key)

    def pending(self) -> int:
        return len(self._jobs)

    @property
    def request_size(self) -> int:
        """Which cached thumbnail size to ask for: 256 unless tiles are big on a high-DPI screen."""
        return 256 if self.tile_px <= 256 else 512

    # -- requests ----------------------------------------------------------------------------------
    def request(self, key: str, entry: FileEntry, *, prefetch_rank: int | None = None) -> bool:
        """Queue a thumbnail unless it is cached, failed, or already queued. True if queued."""
        if self._cache is None or self._backup is None:
            return False
        if key in self._jobs or key in self.lru or key in self._failed:
            return False
        priority = self._next_priority() if prefetch_rank is None else -1 - min(prefetch_rank, 1_000_000)
        job = _ThumbJob(self, self._gen, key, entry, self.request_size, self.tile_px)
        self._jobs[key] = job
        self.pool.start(job, priority)
        return True

    def prune(self, keep: Iterable[str]) -> int:
        """Cancel queued (not yet running) jobs whose keys are not in `keep`. Returns how many."""
        return self._cancel_queued(keep=set(keep))

    def _cancel_queued(self, keep: Iterable[str]) -> int:
        keep = keep if isinstance(keep, set) else set(keep)
        dropped = 0
        with self._lock:
            for key, job in list(self._jobs.items()):
                if key not in keep and not job.started:
                    job.cancelled = True
                    del self._jobs[key]
                    dropped += 1
        return dropped

    def _next_priority(self) -> int:
        if not self._epoch_open:
            self._epoch_open = True
            self._epoch += 1
            self._seq = 0
            if self._epoch > self.MAX_EPOCH:  # practically never; restart the scale, re-request on repaint
                self._cancel_queued(keep=())
                self._epoch = 1
            QTimer.singleShot(0, self._close_epoch)
        self._seq = min(self._seq + 1, self.EPOCH_SPAN - 1)
        return self._epoch * self.EPOCH_SPAN - self._seq

    def _close_epoch(self) -> None:
        self._epoch_open = False

    def _on_finished(self, gen: int, key: str, image: object, state: int) -> None:
        if gen != self._gen:
            return  # from a backup that is no longer shown
        self._jobs.pop(key, None)
        if state == ThumbState.READY and isinstance(image, QImage):
            pm = QPixmap.fromImage(image)
            self.lru.put(key, pm)
        else:
            self._failed[key] = ThumbState(state)
        self.thumbnailReady.emit(key)


class PhotoGridModel(QAbstractListModel):
    """Rows are MediaItems in camera_roll() order (oldest first) or reversed. No pixels live here."""

    def __init__(self, loader: ThumbnailLoader, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.loader = loader
        self.backup: Any = None
        self._items: list[MediaItem] = []
        self._rows: dict[str, int] = {}
        self._newest_first = False
        self._placeholders: dict[ThumbState, QPixmap] = {}
        loader.thumbnailReady.connect(self._thumbnail_ready)

    # -- Qt model API ------------------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemNeverHasChildren

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        row = index.row()
        if not index.isValid() or not 0 <= row < len(self._items):
            return None
        item = self._items[row]
        key = item.entry.file_id
        if role == Qt.ItemDataRole.DecorationRole:
            pm = self.loader.pixmap(key)
            if pm is not None:
                return pm
            state = self.loader.state(key)
            if state == ThumbState.NONE:
                self.loader.request(key, item.entry)
                state = ThumbState.LOADING
            return self._placeholder(state)
        if role == StateRole:
            return self.loader.state(key)
        if role == ItemRole:
            return item
        if role == Qt.ItemDataRole.DisplayRole:
            return item.entry.name
        if role == Qt.ItemDataRole.ToolTipRole:
            kind = "Live Photo" if item.live_video else ("Video" if item.kind == MediaKind.VIDEO else "Photo")
            tip = f"{item.entry.name}\n{kind} · {long_datetime(item.taken)}"
            if self.loader.state(key) == ThumbState.MISSING:
                tip += "\nNot in this backup"
            return tip
        return None

    def _placeholder(self, state: ThumbState) -> QPixmap:
        """What DecorationRole returns before a thumbnail exists (TileDelegate draws its own)."""
        pm = self._placeholders.get(state)
        if pm is None:
            pm = QPixmap(64, 64)
            pm.fill(QColor(128, 128, 128, 40))
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            c = QColor(128, 128, 128, 160)
            r = QRectF(20, 20, 24, 24)
            if state == ThumbState.MISSING:
                icons.paint_missing_glyph(p, r, c)
            elif state == ThumbState.BROKEN:
                icons.paint_broken_glyph(p, r, c)
            else:
                icons.paint_photo_glyph(p, r, c)
            p.end()
            self._placeholders[state] = pm
        return pm

    # -- content -----------------------------------------------------------------------------------
    def set_items(self, backup: Any, items: list[MediaItem], cache: ThumbnailSource | None) -> None:
        self.beginResetModel()
        self.loader.reset(backup, cache)
        self.backup = backup
        self._items = list(reversed(items)) if self._newest_first else list(items)
        self._reindex()
        self.endResetModel()

    def clear(self) -> None:
        self.set_items(None, [], None)

    def _reindex(self) -> None:
        self._rows = {item.entry.file_id: row for row, item in enumerate(self._items)}

    def item(self, row: int) -> MediaItem:
        return self._items[row]

    def items(self) -> list[MediaItem]:
        return list(self._items)

    def row_of(self, file_id: str) -> int | None:
        return self._rows.get(file_id)

    @property
    def newest_first(self) -> bool:
        return self._newest_first

    def set_newest_first(self, newest_first: bool) -> None:
        """Reverse the order in place. Persistent indexes (selection, current, the viewer) follow."""
        if newest_first == self._newest_first:
            return
        self._newest_first = newest_first
        self.layoutAboutToBeChanged.emit()
        n = len(self._items)
        old = self.persistentIndexList()
        new = [self.index(n - 1 - i.row()) if i.isValid() else QModelIndex() for i in old]
        self._items.reverse()
        self._reindex()
        self.changePersistentIndexList(old, new)
        self.layoutChanged.emit()

    def request_rows(self, rows: Iterable[int], *, prefetch: bool = False) -> None:
        for rank, row in enumerate(rows):
            if 0 <= row < len(self._items):
                entry = self._items[row].entry
                self.loader.request(entry.file_id, entry, prefetch_rank=rank if prefetch else None)

    def keys(self, rows: Iterable[int]) -> set[str]:
        return {self._items[r].entry.file_id for r in rows if 0 <= r < len(self._items)}

    def _thumbnail_ready(self, key: str) -> None:
        row = self._rows.get(key)
        if row is not None:
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole, StateRole])

    def summary(self) -> str:
        videos = sum(1 for i in self._items if i.kind == MediaKind.VIDEO)
        photos = len(self._items) - videos
        return f"{count(len(self._items), 'item')} ({count(photos, 'photo')}, {count(videos, 'video')})"

    def year_span(self) -> str:
        years = [y for y in (year(i.taken) for i in self._items) if y is not None]
        if not years:
            return ""
        lo, hi = min(years), max(years)
        return str(lo) if lo == hi else f"{lo} – {hi}"


class TileDelegate(QStyledItemDelegate):
    """Paints one square tile: thumbnail or placeholder, badges, selection."""

    def __init__(self, view: GridView) -> None:
        super().__init__(view)
        self._view = view
        badge_font = QFont(view.font())
        badge_font.setPointSizeF(max(6.5, badge_font.pointSizeF() * 0.74))
        badge_font.setWeight(QFont.Weight.Bold)
        badge_font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
        self._badge_font = badge_font
        note_font = QFont(view.font())
        note_font.setPointSizeF(note_font.pointSizeF() * 0.85)
        self._note_font = note_font

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        return self._view.gridSize()

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index) -> None:
        item: MediaItem | None = index.data(ItemRole)
        if item is None:
            return
        pm = index.data(Qt.ItemDataRole.DecorationRole)  # also triggers the lazy request
        state = index.data(StateRole)
        pal = option.palette
        r = QRect(option.rect).adjusted(GAP // 2, GAP // 2, -(GAP - GAP // 2), -(GAP - GAP // 2))
        rf = QRectF(r)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if state == ThumbState.READY and isinstance(pm, QPixmap):
            dpr = pm.devicePixelRatio() or 1.0
            exact = abs(pm.width() / dpr - r.width()) < 1 and abs(pm.height() / dpr - r.height()) < 1
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, not exact)
            p.drawPixmap(r, pm)
        else:
            self._paint_placeholder(p, rf, pal, state, item)

        if item.kind == MediaKind.VIDEO:
            self._paint_video_badge(p, rf)
        if item.live_video is not None:
            self._paint_live_badge(p, rf)

        if selected:
            veil = QColor(255, 255, 255, 56)
            p.fillRect(rf, veil)
            accent = pal.color(QPalette.ColorRole.Highlight)
            pen = QPen(accent, 3)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rf.adjusted(1.5, 1.5, -1.5, -1.5))
            self._paint_check(p, rf, accent)
        elif hovered:
            p.fillRect(rf, QColor(255, 255, 255, 28))
        p.restore()

    def _paint_placeholder(self, p: QPainter, rf: QRectF, pal: QPalette, state: ThumbState, item: MediaItem) -> None:
        p.fillRect(rf, theme.tile_color(pal))
        ink = QColor(theme.muted_text(pal))
        side = min(rf.width(), rf.height())
        glyph = QRectF(0, 0, side * 0.3, side * 0.3)
        glyph.moveCenter(rf.center() - QPointF(0, side * (0.06 if state == ThumbState.MISSING else 0)))
        if state == ThumbState.MISSING:
            ink.setAlphaF(0.85)
            icons.paint_missing_glyph(p, glyph, ink)
            p.setFont(self._note_font)
            p.setPen(ink)
            fm = QFontMetrics(self._note_font)
            text = fm.elidedText("Not in backup", Qt.TextElideMode.ElideRight, int(rf.width() - 8))
            p.drawText(QRectF(rf.left(), glyph.bottom() + 6, rf.width(), fm.height()), Qt.AlignmentFlag.AlignHCenter, text)
            dash = QPen(ink, 1, Qt.PenStyle.DashLine)
            p.setPen(dash)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rf.adjusted(4.5, 4.5, -4.5, -4.5))
        elif state == ThumbState.BROKEN:
            ink.setAlphaF(0.8)
            icons.paint_broken_glyph(p, glyph, ink)
        else:
            ink.setAlphaF(0.35)
            icons.paint_photo_glyph(p, glyph, ink)

    def _paint_video_badge(self, p: QPainter, rf: QRectF) -> None:
        h = min(36.0, rf.height() * 0.32)
        scrim = QRectF(rf.left(), rf.bottom() - h, rf.width(), h)
        g = QLinearGradient(scrim.topLeft(), scrim.bottomLeft())
        g.setColorAt(0.0, QColor(0, 0, 0, 0))
        g.setColorAt(1.0, QColor(0, 0, 0, 150))
        p.fillRect(scrim, g)
        icons.paint_play(p, QPointF(rf.left() + 14, rf.bottom() - 12), 11, QColor("white"))

    def _paint_live_badge(self, p: QPainter, rf: QRectF) -> None:
        fm = QFontMetrics(self._badge_font)
        text_w = fm.horizontalAdvance("LIVE")
        h = fm.height() + 4
        pill = QRectF(rf.left() + 6, rf.top() + 6, h + text_w + 8, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawRoundedRect(pill, h / 2, h / 2)
        icons.paint_live(p, QPointF(pill.left() + h / 2 + 1, pill.center().y()), h * 0.36, QColor("white"))
        p.setFont(self._badge_font)
        p.setPen(QColor("white"))
        p.drawText(QRectF(pill.left() + h, pill.top(), text_w + 4, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "LIVE")

    def _paint_check(self, p: QPainter, rf: QRectF, accent: QColor) -> None:
        c = QPointF(rf.right() - 15, rf.bottom() - 15)
        p.setPen(QPen(QColor("white"), 1.6))
        p.setBrush(accent)
        p.drawEllipse(c, 9.5, 9.5)
        pen = QPen(QColor("white"), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline([QPointF(c.x() - 4.2, c.y() + 0.2), QPointF(c.x() - 1.2, c.y() + 3.2), QPointF(c.x() + 4.4, c.y() - 3.2)])


class GridView(QListView):
    """IconMode, uniform square tiles that stretch to fill each row, eased wheel scrolling."""

    visibleRangeChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setUniformItemSizes(True)
        self.setMovement(QListView.Movement.Static)
        # Fixed, not Adjust: Adjust relays out every item on every resize event, which stutters with tens
        # of thousands of tiles. fit_grid() changes the grid size instead, and relayout follows from that.
        self.setResizeMode(QListView.ResizeMode.Fixed)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setSpacing(0)
        self.setLayoutMode(QListView.LayoutMode.SinglePass)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionRectVisible(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setDragEnabled(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setGridSize(QSize(MIN_TILE, MIN_TILE))
        self._scroll_anim = QVariantAnimation(self)
        self._scroll_anim.setDuration(170)
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.valueChanged.connect(lambda v: self.verticalScrollBar().setValue(int(v)))
        self._scroll_target: float | None = None
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self.fit_grid)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.visibleRangeChanged.emit())

    # -- layout ------------------------------------------------------------------------------------
    def tile_size(self) -> int:
        return self.gridSize().width()

    def layout_width(self) -> int:
        """The width QListView wraps items in. It mirrors QListViewPrivate::prepareItemsLayout: the
        viewport without scroll bars, minus the scroll bar's extent while the policy is AsNeeded (so
        nothing reflows when the bar appears), and an item wraps once x + width > right() = width - 1."""
        width = self.maximumViewportSize().width()
        if self.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded:
            width -= self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent, None, self.verticalScrollBar())
        return width - 1

    def fit_grid(self) -> None:
        width = self.layout_width()
        if width <= 0:
            return
        cols = max(1, width // MIN_TILE)
        side = max(MIN_TILE // 2, width // cols)
        if self.gridSize() != QSize(side, side):
            self.setGridSize(QSize(side, side))
            self.visibleRangeChanged.emit()

    LARGE = 5000  # rows; above this a relayout is noticeable, so wait for the resize to settle

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        model = self.model()
        if model is not None and model.rowCount() > self.LARGE and self.isVisible():
            self._fit_timer.start(120)
        else:
            self.fit_grid()
        self.visibleRangeChanged.emit()

    def updateGeometries(self) -> None:  # noqa: N802
        super().updateGeometries()
        # Qt's per-pixel step is a whole row; a third of a tile per wheel line reads as smooth.
        self.verticalScrollBar().setSingleStep(max(16, self.tile_size() // 3))

    def columns(self) -> int:
        return max(1, self.layout_width() // max(1, self.gridSize().width()))

    def visible_rows(self, margin_screens: float = 0.0) -> range:
        model = self.model()
        n = model.rowCount() if model is not None else 0
        if n == 0:
            return range(0)
        tile = max(1, self.gridSize().height())
        cols = self.columns()
        extra = int(self.viewport().height() * margin_screens)
        top = max(0, self.verticalScrollBar().value() - extra)
        bottom = self.verticalScrollBar().value() + self.viewport().height() + extra - 1
        first = (top // tile) * cols
        last = (bottom // tile + 1) * cols
        return range(min(n, first), min(n, last))

    # -- eased wheel scrolling ---------------------------------------------------------------------
    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.pixelDelta().isNull() and not event.modifiers() and event.angleDelta().y():
            bar = self.verticalScrollBar()
            base = self._scroll_target if self._scroll_anim.state() == QVariantAnimation.State.Running and self._scroll_target is not None else bar.value()
            lines = event.angleDelta().y() / 120 * 3
            target = max(bar.minimum(), min(bar.maximum(), base - lines * bar.singleStep()))
            self._scroll_target = target
            self._scroll_anim.stop()
            self._scroll_anim.setStartValue(float(bar.value()))
            self._scroll_anim.setEndValue(float(target))
            self._scroll_anim.start()
            event.accept()
            return
        super().wheelEvent(event)  # touchpads already send smooth pixel deltas


class PhotoGrid(QWidget):
    """Header (title, date of what's on screen, sort toggle, Export…) above the grid view."""

    activated = Signal(int)  # row
    exportRequested = Signal(list)  # [MediaItem]
    selectionCountChanged = Signal(int)

    def __init__(self, loader: ThumbnailLoader | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.loader = loader or ThumbnailLoader(self)
        self.model = PhotoGridModel(self.loader, self)
        self.view = GridView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(TileDelegate(self.view))
        self.view.activated.connect(lambda idx: self.activated.emit(idx.row()))
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.selectionModel().selectionChanged.connect(self._selection_changed)
        self.view.visibleRangeChanged.connect(self._visible_range_changed)
        self.model.modelReset.connect(self._selection_changed)

        self.title = QLabel("Camera Roll")
        tf = QFont(self.font())
        tf.setPointSizeF(tf.pointSizeF() * 1.35)
        tf.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(tf)
        self.subtitle = QLabel("")
        self.when = QLabel("")
        wf = QFont(self.font())
        wf.setWeight(QFont.Weight.DemiBold)
        self.when.setFont(wf)
        self.sort_button = QToolButton()
        self.sort_button.setCheckable(True)
        self.sort_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.sort_button.setAutoRaise(True)
        self.sort_button.toggled.connect(self.set_newest_first)
        self.export_button = QPushButton("Export…")
        self.export_button.setEnabled(False)
        self.export_button.setToolTip("Copy the selected photos and videos to a folder")
        self.export_button.clicked.connect(self.request_export)

        head = QHBoxLayout()
        head.setContentsMargins(16, 12, 12, 10)
        head.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        head.addLayout(titles)
        head.addStretch(1)
        head.addWidget(self.when, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addSpacing(8)
        head.addWidget(self.sort_button, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.export_button, 0, Qt.AlignmentFlag.AlignVCenter)
        header = QWidget()
        header.setLayout(head)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(header)
        col.addWidget(self.view, 1)

        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(90)
        self._settle.timeout.connect(self._scroll_settled)
        self._sync_sort_text()
        self._built = True
        self._tint()

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == event.Type.PaletteChange and hasattr(self, "_built"):
            self._tint()
        super().changeEvent(event)

    def _tint(self) -> None:
        pal = self.subtitle.palette()
        pal.setColor(QPalette.ColorRole.WindowText, theme.muted_text(self.palette()))
        self.subtitle.setPalette(pal)

    # -- content -----------------------------------------------------------------------------------
    def set_media(self, backup: Any, items: list[MediaItem], cache: ThumbnailSource | None, title: str = "Camera Roll") -> None:
        self.loader.tile_px = self._tile_px()
        self.model.set_items(backup, items, cache)
        self.title.setText(title)
        span = self.model.year_span()
        self.subtitle.setText(count(len(items), "photo or video", "photos and videos") + (f" · {span}" if span else ""))
        self.view.scrollToTop()
        self._visible_range_changed()

    def clear(self) -> None:
        self.model.clear()
        self.when.setText("")

    def selected_rows(self) -> list[int]:
        # From the selection ranges, not selectedIndexes(): Select All on 60k items is one range, and
        # building 60k QModelIndex wrappers took over half a second.
        rows: set[int] = set()
        for rng in self.view.selectionModel().selection():
            rows.update(range(rng.top(), rng.bottom() + 1))
        return sorted(rows)

    def selected_items(self) -> list[MediaItem]:
        return [self.model.item(r) for r in self.selected_rows()]

    def selected_count(self) -> int:
        return len(self.selected_rows())

    # -- sort --------------------------------------------------------------------------------------
    def set_newest_first(self, newest_first: bool) -> None:
        if self.sort_button.isChecked() != newest_first:
            self.sort_button.setChecked(newest_first)  # re-enters via toggled
            return
        rows = self.view.visible_rows()
        anchor = QPersistentModelIndex(self.model.index(rows.start)) if len(rows) else None
        self.model.set_newest_first(newest_first)
        self._sync_sort_text()
        if anchor is not None and anchor.isValid():
            self.view.scrollTo(self.model.index(anchor.row()), QAbstractItemView.ScrollHint.PositionAtTop)
        self._visible_range_changed()

    def _sync_sort_text(self) -> None:
        newest = self.model.newest_first
        self.sort_button.setText("↓  Newest first" if newest else "↑  Oldest first")
        self.sort_button.setToolTip("Click to show " + ("oldest" if newest else "newest") + " first")

    # -- export ------------------------------------------------------------------------------------
    def request_export(self) -> None:
        items = self.selected_items()
        if items:
            self.exportRequested.emit(items)

    def _context_menu(self, pos: QPoint) -> None:
        idx = self.view.indexAt(pos)
        sel = self.view.selectionModel()
        if idx.isValid() and not sel.isSelected(idx):
            sel.select(idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            sel.setCurrentIndex(idx, QItemSelectionModel.SelectionFlag.NoUpdate)
        menu = QMenu(self)
        n = self.selected_count()
        if idx.isValid():
            open_action = QAction("Open", menu)
            open_action.triggered.connect(lambda: self.activated.emit(idx.row()))
            menu.addAction(open_action)
        export_action = QAction(f"Export {count(n, 'item')}…" if n else "Export…", menu)
        export_action.setEnabled(n > 0)
        export_action.triggered.connect(self.request_export)
        menu.addAction(export_action)
        menu.addSeparator()
        select_all = QAction("Select All", menu)
        select_all.triggered.connect(self.view.selectAll)
        menu.addAction(select_all)
        self._last_menu = menu  # tests inspect it
        menu.popup(self.view.viewport().mapToGlobal(pos))

    def _selection_changed(self, *_args) -> None:
        n = self.selected_count()
        self.export_button.setEnabled(n > 0)
        self.export_button.setText(f"Export {n:,}…" if n > 1 else "Export…")
        self.selectionCountChanged.emit(n)

    # -- visible range: date label, pruning and prefetch -------------------------------------------
    def _tile_px(self) -> int:
        dpr = self.view.devicePixelRatioF() or 1.0
        return max(32, round((self.view.tile_size() - GAP) * dpr))

    def _visible_range_changed(self) -> None:
        rows = self.view.visible_rows()
        if len(rows):
            self.when.setText(month_year(self.model.item(rows.start).taken))
        else:
            self.when.setText("")
        self.loader.tile_px = self._tile_px()
        self._settle.start()

    def _scroll_settled(self) -> None:
        visible = self.view.visible_rows()
        if not len(visible):
            return
        nearby = self.view.visible_rows(margin_screens=1.0)
        self.loader.prune(self.model.keys(nearby))
        # Warm the next screen in both directions, nearest rows first, below everything visible.
        after = range(visible.stop, nearby.stop)
        before = range(visible.start - 1, nearby.start - 1, -1)
        order = [r for pair in zip(after, before) for r in pair]
        order += list(after[len(before):]) + list(before[len(after):])
        self.model.request_rows(order, prefetch=True)
