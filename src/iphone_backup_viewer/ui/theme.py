"""Fusion with a calm light/dark palette that follows the OS colour scheme (Qt 6.5+ reports it)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

_LIGHT = {
    "window": "#f2f2f5",
    "base": "#ffffff",
    "alt": "#f6f6f8",
    "text": "#1d1d1f",
    "muted": "#6e6e76",
    "disabled": "#a8a8ae",
    "button": "#fbfbfd",
    "light": "#ffffff",
    "mid": "#c9c9cf",
    "dark": "#a0a0a8",
    "shadow": "#8a8a90",
    "highlight": "#0a6cff",
    "link": "#0a6cff",
    "tooltip": "#ffffff",
}
_DARK = {
    "window": "#202024",
    "base": "#161619",
    "alt": "#1c1c20",
    "text": "#ececf1",
    "muted": "#9c9ca6",
    "disabled": "#62626a",
    "button": "#2c2c31",
    "light": "#3a3a40",
    "mid": "#34343a",
    "dark": "#121214",
    "shadow": "#000000",
    "highlight": "#0a84ff",
    "link": "#4da3ff",
    "tooltip": "#2c2c31",
}


def is_dark(app: QGuiApplication | None = None) -> bool:
    app = app or QGuiApplication.instance()
    if app is None:
        return False
    scheme = app.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


def build_palette(dark: bool) -> QPalette:
    c = {k: QColor(v) for k, v in (_DARK if dark else _LIGHT).items()}
    p = QPalette()
    R, G = QPalette.ColorRole, QPalette.ColorGroup
    roles = {
        R.Window: c["window"],
        R.WindowText: c["text"],
        R.Base: c["base"],
        R.AlternateBase: c["alt"],
        R.ToolTipBase: c["tooltip"],
        R.ToolTipText: c["text"],
        R.PlaceholderText: c["muted"],
        R.Text: c["text"],
        R.Button: c["button"],
        R.ButtonText: c["text"],
        R.BrightText: QColor("#ff453a"),
        R.Light: c["light"],
        R.Midlight: c["mid"].lighter(110),
        R.Mid: c["mid"],
        R.Dark: c["dark"],
        R.Shadow: c["shadow"],
        R.Highlight: c["highlight"],
        R.HighlightedText: QColor("#ffffff"),
        R.Link: c["link"],
        R.LinkVisited: c["link"].darker(115),
        R.Accent: c["highlight"],
    }
    for role, color in roles.items():
        p.setColor(role, color)
    for role in (R.WindowText, R.Text, R.ButtonText, R.PlaceholderText):
        p.setColor(G.Disabled, role, c["disabled"])
    p.setColor(G.Disabled, R.Highlight, c["mid"])
    p.setColor(G.Inactive, R.Highlight, c["highlight"])
    return p


def apply_theme(app: QGuiApplication) -> None:
    """Fusion + our palette, re-applied whenever the OS switches between light and dark."""
    app.setStyle("Fusion")
    app.setPalette(build_palette(is_dark(app)))

    def _scheme_changed(scheme: Qt.ColorScheme) -> None:
        if scheme != Qt.ColorScheme.Unknown:
            app.setPalette(build_palette(scheme == Qt.ColorScheme.Dark))

    app.styleHints().colorSchemeChanged.connect(_scheme_changed)


def mix(a: QColor, b: QColor, t: float) -> QColor:
    """Linear blend: t=0 gives a, t=1 gives b."""
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def muted_text(palette: QPalette) -> QColor:
    return mix(palette.color(QPalette.ColorRole.Text), palette.color(QPalette.ColorRole.Base), 0.42)


def tile_color(palette: QPalette) -> QColor:
    """Neutral fill for a thumbnail that is not decoded yet."""
    return mix(palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text), 0.08)


def viewer_palette() -> QPalette:
    """Photos look best on near-black, whatever the OS scheme."""
    p = build_palette(True)
    p.setColor(QPalette.ColorRole.Window, QColor("#0d0d0f"))
    p.setColor(QPalette.ColorRole.Base, QColor("#0d0d0f"))
    return p
