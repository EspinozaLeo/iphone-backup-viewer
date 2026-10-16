"""Small vector glyphs drawn with QPainter, so the app ships no image assets. GUI thread only."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)


def _pen(
    color: QColor,
    width: float,
    cap: Qt.PenCapStyle = Qt.PenCapStyle.SquareCap,
    join: Qt.PenJoinStyle = Qt.PenJoinStyle.BevelJoin,
) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(cap)
    pen.setJoinStyle(join)
    return pen


def _canvas(w: int, h: int | None = None, dpr: float | None = None) -> tuple[QPixmap, QPainter]:
    h = w if h is None else h
    if dpr is None:
        app = QGuiApplication.instance()
        dpr = app.devicePixelRatio() if app is not None else 1.0
    pm = QPixmap(round(w * dpr), round(h * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pm, p


def paint_lock(p: QPainter, rect: QRectF, color: QColor) -> None:
    w, h = rect.width(), rect.height()
    body = QRectF(rect.left() + w * 0.18, rect.top() + h * 0.45, w * 0.64, h * 0.5)
    pen = _pen(color, max(1.2, w * 0.11))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    shackle = QPainterPath()
    sx0, sx1 = rect.left() + w * 0.32, rect.left() + w * 0.68
    top = rect.top() + h * 0.1
    shackle.moveTo(sx0, body.top())
    shackle.lineTo(sx0, top + w * 0.18)
    shackle.arcTo(QRectF(sx0, top, sx1 - sx0, (sx1 - sx0)), 180, -180)
    shackle.lineTo(sx1, body.top())
    p.drawPath(shackle)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(body, w * 0.1, w * 0.1)


def lock_pixmap(size: int, color: QColor) -> QPixmap:
    pm, p = _canvas(size)
    paint_lock(p, QRectF(0, 0, size, size), color)
    p.end()
    return pm


def paint_phone(p: QPainter, rect: QRectF, color: QColor) -> None:
    w, h = rect.width(), rect.height()
    body = QRectF(rect.left() + w * 0.24, rect.top() + h * 0.06, w * 0.52, h * 0.88)
    p.setPen(_pen(color, max(1.2, w * 0.07)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(body, w * 0.11, w * 0.11)
    p.setPen(_pen(color, max(1.2, w * 0.07), cap=Qt.PenCapStyle.RoundCap))
    y = body.top() + h * 0.09
    p.drawLine(QPointF(body.center().x() - w * 0.07, y), QPointF(body.center().x() + w * 0.07, y))


def phone_pixmap(size: int, color: QColor) -> QPixmap:
    pm, p = _canvas(size)
    paint_phone(p, QRectF(0, 0, size, size), color)
    p.end()
    return pm


def paint_play(p: QPainter, center: QPointF, size: float, color: QColor) -> None:
    """A rounded play triangle, `size` tall, optically centred on `center`."""
    h = size
    w = size * 0.9
    x0 = center.x() - w * 0.4
    path = QPainterPath()
    path.moveTo(x0, center.y() - h / 2)
    path.lineTo(x0 + w, center.y())
    path.lineTo(x0, center.y() + h / 2)
    path.closeSubpath()
    p.setPen(_pen(color, max(1.0, size * 0.12), join=Qt.PenJoinStyle.RoundJoin))
    p.setBrush(color)
    p.drawPath(path)


def paint_live(p: QPainter, center: QPointF, radius: float, color: QColor) -> None:
    """The Live Photo mark: a dot inside a ring inside a dotted ring."""
    p.setBrush(color)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(center, radius * 0.28, radius * 0.28)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(_pen(color, max(1.0, radius * 0.14)))
    p.drawEllipse(center, radius * 0.6, radius * 0.6)
    dot = _pen(color, max(1.0, radius * 0.16), cap=Qt.PenCapStyle.RoundCap)
    dot.setDashPattern([0.01, 2.2])
    p.setPen(dot)
    p.drawEllipse(center, radius * 0.95, radius * 0.95)


def paint_photo_glyph(p: QPainter, rect: QRectF, color: QColor) -> None:
    """A framed landscape (mountains + sun): the 'picture goes here' mark for placeholders."""
    w = rect.width()
    pen = _pen(color, max(1.2, w * 0.06), join=Qt.PenJoinStyle.RoundJoin, cap=Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    frame = rect.adjusted(w * 0.06, w * 0.16, -w * 0.06, -w * 0.16)
    p.drawRoundedRect(frame, w * 0.08, w * 0.08)
    m = QPainterPath()
    m.moveTo(frame.left() + w * 0.06, frame.bottom() - w * 0.08)
    m.lineTo(frame.left() + w * 0.34, frame.top() + frame.height() * 0.45)
    m.lineTo(frame.left() + w * 0.52, frame.bottom() - frame.height() * 0.3)
    m.lineTo(frame.left() + w * 0.66, frame.top() + frame.height() * 0.6)
    m.lineTo(frame.right() - w * 0.06, frame.bottom() - w * 0.08)
    p.drawPath(m)
    p.setBrush(color)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(frame.right() - w * 0.22, frame.top() + w * 0.2), w * 0.065, w * 0.065)


def paint_missing_glyph(p: QPainter, rect: QRectF, color: QColor) -> None:
    """A dog-eared page with a slash: the file is listed but its data is not in the backup."""
    w = rect.width()
    pen = _pen(color, max(1.2, w * 0.06), join=Qt.PenJoinStyle.RoundJoin, cap=Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    page = rect.adjusted(w * 0.22, w * 0.1, -w * 0.22, -w * 0.1)
    ear = w * 0.18
    path = QPainterPath()
    path.moveTo(page.left(), page.top())
    path.lineTo(page.right() - ear, page.top())
    path.lineTo(page.right(), page.top() + ear)
    path.lineTo(page.right(), page.bottom())
    path.lineTo(page.left(), page.bottom())
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QPointF(rect.left() + w * 0.12, rect.bottom() - w * 0.12), QPointF(rect.right() - w * 0.12, rect.top() + w * 0.12))


def paint_broken_glyph(p: QPainter, rect: QRectF, color: QColor) -> None:
    """An exclamation mark in a circle: the file is there but could not be decoded."""
    w = rect.width()
    p.setPen(_pen(color, max(1.2, w * 0.07)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    c = rect.center()
    p.drawEllipse(c, w * 0.38, w * 0.38)
    p.setPen(_pen(color, max(1.5, w * 0.09), cap=Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(c.x(), c.y() - w * 0.18), QPointF(c.x(), c.y() + w * 0.04))
    p.drawPoint(QPointF(c.x(), c.y() + w * 0.18))


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm, p = _canvas(size, dpr=1.0)
        r = QRectF(0, 0, size, size).adjusted(size * 0.04, size * 0.04, -size * 0.04, -size * 0.04)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0.0, QColor("#ff9f43"))
        g.setColorAt(0.5, QColor("#ee5a8f"))
        g.setColorAt(1.0, QColor("#5b5bf0"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRoundedRect(r, size * 0.22, size * 0.22)
        paint_photo_glyph(p, r.adjusted(size * 0.14, size * 0.14, -size * 0.14, -size * 0.14), QColor("white"))
        p.end()
        icon.addPixmap(pm)
    return icon


def _paint_glyph(p: QPainter, kind: str, s: float, color: QColor) -> None:
    pen = _pen(color, max(1.6, s * 0.11), cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin)
    if kind in ("chevron-left", "chevron-right"):
        p.setPen(pen)
        x0, x1 = (s * 0.62, s * 0.36) if kind == "chevron-left" else (s * 0.38, s * 0.64)
        p.drawPolyline(QPolygonF([QPointF(x0, s * 0.2), QPointF(x1, s * 0.5), QPointF(x0, s * 0.8)]))
    elif kind == "play":
        paint_play(p, QPointF(s * 0.5, s * 0.5), s * 0.56, color)
    elif kind == "pause":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        w = s * 0.16
        p.drawRoundedRect(QRectF(s * 0.27, s * 0.2, w, s * 0.6), w * 0.35, w * 0.35)
        p.drawRoundedRect(QRectF(s * 0.57, s * 0.2, w, s * 0.6), w * 0.35, w * 0.35)
    elif kind in ("volume", "mute"):
        body = QPainterPath()
        body.moveTo(s * 0.12, s * 0.38)
        body.lineTo(s * 0.27, s * 0.38)
        body.lineTo(s * 0.47, s * 0.2)
        body.lineTo(s * 0.47, s * 0.8)
        body.lineTo(s * 0.27, s * 0.62)
        body.lineTo(s * 0.12, s * 0.62)
        body.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(body)
        p.setPen(_pen(color, max(1.3, s * 0.08), cap=Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if kind == "volume":
            p.drawArc(QRectF(s * 0.38, s * 0.32, s * 0.24, s * 0.36), -60 * 16, 120 * 16)
            p.drawArc(QRectF(s * 0.36, s * 0.18, s * 0.44, s * 0.64), -60 * 16, 120 * 16)
        else:
            p.drawLine(QPointF(s * 0.6, s * 0.38), QPointF(s * 0.84, s * 0.62))
            p.drawLine(QPointF(s * 0.84, s * 0.38), QPointF(s * 0.6, s * 0.62))


def glyph_icon(kind: str, color: QColor, size: int = 20) -> QIcon:
    """Light-on-dark control icons for the viewer: chevron-left/right, play, pause, volume, mute."""
    icon = QIcon()
    for mode, alpha in ((QIcon.Mode.Normal, 1.0), (QIcon.Mode.Active, 1.0), (QIcon.Mode.Disabled, 0.3)):
        c = QColor(color)
        c.setAlphaF(c.alphaF() * alpha)
        pm, p = _canvas(size)
        _paint_glyph(p, kind, size, c)
        p.end()
        icon.addPixmap(pm, mode)
    return icon
