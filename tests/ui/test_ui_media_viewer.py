import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtMultimedia import QMediaPlayer

from iphone_backup_viewer.core.models import MediaKind
from iphone_backup_viewer.ui import playback
from iphone_backup_viewer.ui.media_viewer import pil_to_qimage


def _row(model, pred):
    return next(r for r in range(model.rowCount()) if pred(model.item(r)))


@pytest.fixture
def opened(make_window, fake, wait_grid):
    w = make_window(fake.services)
    wait_grid(w)
    return w


def test_double_click_opens_viewer_and_arrows_navigate(opened, qtbot):
    w = opened
    view, model = w.grid.view, w.grid.model
    pos = view.visualRect(model.index(2)).center()
    # A real double-click is press/release then a double-click event; QTest.mouseDClick sends only the last.
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    qtbot.waitUntil(lambda: w.viewer is not None and w.viewer.isVisible())
    viewer = w.viewer
    assert viewer.current_row() == 2
    assert viewer.title.text() == model.item(2).entry.name
    qtbot.keyClick(viewer, Qt.Key.Key_Right)
    assert viewer.current_row() == 3
    assert viewer.title.text() == model.item(3).entry.name
    assert viewer.position.text() == f"4 of {model.rowCount()}"
    qtbot.keyClick(viewer, Qt.Key.Key_Left)
    qtbot.keyClick(viewer, Qt.Key.Key_Left)
    assert viewer.current_row() == 1
    viewer.show_item(model, 0)
    qtbot.keyClick(viewer, Qt.Key.Key_Left)  # stops at the first item
    assert viewer.current_row() == 0
    assert not viewer.prev_button.isEnabled()
    qtbot.keyClick(viewer, Qt.Key.Key_Escape)
    assert not viewer.isVisible()


def test_photo_decodes_in_worker_and_fits(opened, qtbot):
    w = opened
    model = w.grid.model
    w.open_viewer(0)
    viewer = w.viewer
    viewer.resize(500, 400)
    qtbot.waitUntil(lambda: viewer.canvas.image() is not None)
    image = viewer.canvas.image()
    assert image.width() == 960 and image.height() == 720
    fitted = viewer.canvas.fitted_size()
    assert fitted.width() <= viewer.canvas.width() and fitted.height() <= viewer.canvas.height()
    assert abs(fitted.width() / fitted.height() - 960 / 720) < 0.02
    # The neighbour was prefetched, so stepping to it shows a picture without waiting.
    qtbot.waitUntil(lambda: model.item(1).entry.file_id in viewer._decoded)
    viewer.next()
    assert viewer.canvas.image() is not None


def test_missing_photo_shows_message(opened, library, qtbot):
    w = opened
    row = _row(w.grid.model, lambda i: i.entry.file_id in library.missing)
    w.open_viewer(row)
    qtbot.waitUntil(lambda: "isn't in the backup" in w.viewer.canvas.message())


def test_viewer_follows_sort_order(opened, library):
    w = opened
    w.grid.set_newest_first(True)
    w.open_viewer(0)
    assert w.viewer.current_item() == library.items[-1]


def test_video_plays_from_extensionless_file(opened, qtbot):
    w = opened
    row = _row(w.grid.model, lambda i: i.kind == MediaKind.VIDEO)
    w.open_viewer(row)
    viewer = w.viewer
    qtbot.waitUntil(lambda: viewer.player is not None and viewer.player.duration() > 0, timeout=8000)
    source = Path(viewer.player.source().toLocalFile())
    assert source.suffix == ""  # the backup's own data file, no alias needed
    assert viewer.controls.isVisible()
    assert viewer.total.text() == "0:01"
    assert viewer.stack.currentWidget() is viewer._video
    viewer.next()
    assert viewer.player.source().isEmpty()  # stopped and released when moving on


def test_live_photo_plays_motion_then_returns_to_still(opened, qtbot):
    w = opened
    row = _row(w.grid.model, lambda i: i.live_video is not None)
    item = w.grid.model.item(row)
    w.open_viewer(row)
    viewer = w.viewer
    assert viewer.live_button.isVisible()
    qtbot.waitUntil(lambda: viewer.canvas.image() is not None)
    viewer.live_button.click()
    qtbot.waitUntil(lambda: viewer.player is not None and not viewer.player.source().isEmpty(), timeout=5000)
    assert viewer.player.source().toLocalFile().endswith(item.live_video.file_id)
    assert not viewer.controls.isVisible()
    qtbot.waitUntil(lambda: viewer.stack.currentWidget() is viewer.canvas and viewer.player.source().isEmpty(), timeout=8000)
    assert viewer.canvas.image() is not None


def test_player_error_retries_through_extension_alias(opened, qtbot, tmp_path):
    w = opened
    row = _row(w.grid.model, lambda i: i.kind == MediaKind.VIDEO)
    entry = w.grid.model.item(row).entry
    w.open_viewer(row)
    viewer = w.viewer
    qtbot.waitUntil(lambda: viewer.player is not None and not viewer.player.source().isEmpty(), timeout=5000)
    viewer._player_error(QMediaPlayer.Error.FormatError, "simulated: backend wants an extension")
    alias = playback.play_dir(w.services.thumbnails.cache_dir, w.backup.info.udid) / f"{entry.file_id}.mov"
    qtbot.waitUntil(lambda: Path(viewer.player.source().toLocalFile()) == alias, timeout=5000)
    assert alias.read_bytes() == w.backup.local_path(entry).read_bytes()
    viewer._player_error(QMediaPlayer.Error.FormatError, "still failing")  # second failure: give up politely
    assert viewer.stack.currentWidget() is viewer.canvas
    assert "Can't play this video" in viewer.canvas.message()


def test_playable_alias_links_or_copies(tmp_path, monkeypatch):
    src = tmp_path / "backup" / "ab" / "abcdef"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"\x00\x00\x00\x14ftypqt  " + b"x" * 100)
    cache = tmp_path / "cache"
    linked = playback.playable_alias(src, cache, "UDID", "abcdef", ".MOV")
    assert linked == cache / "UDID" / "play" / "abcdef.mov"
    assert linked.read_bytes() == src.read_bytes()
    assert os.stat(linked).st_nlink == 2  # a hard link on the same volume
    assert playback.playable_alias(src, cache, "UDID", "abcdef", ".MOV") == linked  # reused

    def no_links(*_a, **_k):
        raise OSError("cross-device link")

    monkeypatch.setattr(os, "link", no_links)
    copied = playback.playable_alias(src, cache, "UDID2", "abcdef", "mp4")
    assert copied.name == "abcdef.mp4"
    assert copied.read_bytes() == src.read_bytes()
    assert os.stat(copied).st_nlink == 1
    assert not list(copied.parent.glob("*.part"))
    assert src.stat().st_nlink == 2  # the backup file itself is untouched


def test_pil_to_qimage_handles_modes():
    from PIL import Image

    for mode, colour in (("RGB", (10, 20, 30)), ("RGBA", (10, 20, 30, 128)), ("L", 77), ("P", 3)):
        img = Image.new(mode, (7, 5), colour)  # odd width: no row-padding assumptions
        q = pil_to_qimage(img)
        assert (q.width(), q.height()) == (7, 5)
        assert not q.isNull()
    q = pil_to_qimage(Image.new("RGB", (3, 2), (10, 20, 30)))
    c = q.pixelColor(2, 1)
    assert (c.red(), c.green(), c.blue()) == (10, 20, 30)
