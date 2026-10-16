"""Dev script: run the UI over the test fakes and save screenshots (not collected by pytest).

    python tests/ui/screenshot_demo.py OUT_DIR [--dark] [--many N]
    python tests/ui/screenshot_demo.py --interactive [--many N]     # click around a fake library

Needs PYTHONPATH=src. Screenshots render offscreen unless --interactive.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", type=Path)
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--interactive", action="store_true")
    ap.add_argument("--many", type=int, default=0, help="repeat the items to N rows (scroll/perf check)")
    args = ap.parse_args()
    if not args.interactive:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        if sys.platform == "win32":  # the offscreen plugin's FreeType font database needs to be told where fonts live
            os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

    import conftest
    from PySide6.QtCore import QItemSelectionModel, Qt
    from PySide6.QtWidgets import QApplication

    from iphone_backup_viewer.ui import icons, theme
    from iphone_backup_viewer.ui.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    if not args.interactive and sys.platform == "win32":
        from PySide6.QtGui import QFont

        app.setFont(QFont("Segoe UI", 9))  # what the Windows platform theme would pick
    app.setApplicationName("iPhoneBackupViewer")
    app.setOrganizationName("iPhoneBackupViewer")
    theme.apply_theme(app)
    if args.dark:
        app.setPalette(theme.build_palette(True))
    app.setWindowIcon(icons.app_icon())

    tmp = Path(tempfile.mkdtemp(prefix="ibv-demo-"))
    lib = conftest.build_library(tmp)
    if args.many:  # unique file_ids ("<sha1>-<n>") that FakeBackup maps back to the same data file
        base, many = list(lib.items), []
        for i in range(args.many):
            it = base[i % len(base)]
            live = replace(it.live_video, file_id=f"{it.live_video.file_id}-{i}") if it.live_video else None
            many.append(replace(it, entry=replace(it.entry, file_id=f"{it.entry.file_id}-{i}"), live_video=live))
        lib.items[:] = many
    fake = conftest.make_services(lib, tmp / "cache")

    def pump(cond, timeout=10.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if cond():
                return True
            time.sleep(0.01)
        return False

    window = MainWindow(fake.services)
    window.resize(1280, 820)
    window.show()
    if args.interactive:
        return app.exec()

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    suffix = "-dark" if args.dark else ""

    def shot(widget, name: str) -> None:
        pump(lambda: False, 0.15)
        path = out / f"{name}{suffix}.png"
        widget.grab().save(str(path))
        print("saved", path)

    grid = window.grid
    assert pump(lambda: grid.model.rowCount() > 0), "grid never filled"

    def visible_ready() -> bool:
        rows = grid.view.visible_rows()
        from iphone_backup_viewer.ui.photo_grid import StateRole, ThumbState

        return all(grid.model.index(r).data(StateRole) not in (ThumbState.NONE, ThumbState.LOADING) for r in rows)

    pump(visible_ready, 20)
    shot(window, "main")

    sel = grid.view.selectionModel()
    for r in (1, 2, 6):
        sel.select(grid.model.index(r), QItemSelectionModel.SelectionFlag.Select)
    grid.view.scrollTo(grid.model.index(min(14, grid.model.rowCount() - 1)))
    pump(visible_ready, 10)
    shot(grid, "grid")
    sel.clear()

    live_row = next(r for r in range(grid.model.rowCount()) if grid.model.item(r).live_video is not None)
    window.open_viewer(live_row)
    viewer = window.viewer
    viewer.resize(1100, 760)
    pump(lambda: viewer.canvas.image() is not None, 10)
    shot(viewer, "viewer-live")

    video_row = next(r for r in range(grid.model.rowCount()) if grid.model.item(r).kind.value == "video")
    viewer.show_item(grid.model, video_row)
    pump(lambda: viewer.player is not None and viewer.player.duration() > 0, 10)
    pump(lambda: viewer._video.videoSink().videoFrame().isValid(), 5)
    viewer.player.pause()
    pump(lambda: False, 0.3)
    # QVideoWidget draws into a native child surface that widget.grab() can't capture offscreen, so paint
    # the frame the widget's sink is showing into the shot, at the widget's spot, scaled to fit.
    frame = viewer._video.videoSink().videoFrame()
    print("video sink frame valid:", frame.isValid(), frame.size())
    img = viewer.grab()
    if frame.isValid():
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtGui import QPainter

        area = QRect(viewer._video.mapTo(viewer, QPoint(0, 0)), viewer._video.size())
        still = frame.toImage().scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        p = QPainter(img)
        p.drawImage(area.left() + (area.width() - still.width()) // 2, area.top() + (area.height() - still.height()) // 2, still)
        p.end()
    img.save(str(out / f"viewer-video{suffix}.png"))
    print("saved", out / f"viewer-video{suffix}.png")
    viewer.close()

    window.sidebar.select_row(next(r for r, b in enumerate(window.sidebar.model.backups()) if b.is_encrypted))
    shot(window, "encrypted")

    empty = MainWindow(conftest.make_services(lib, tmp / "cache2", backups=[]).services)
    empty.resize(1000, 640)
    empty.show()
    pump(lambda: empty.central.currentWidget() is empty.empty_page)
    shot(empty, "empty")

    for w in (window, empty):
        w.shutdown()
        w.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
