import threading

from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtGui import QPixmap

from iphone_backup_viewer.core.models import MediaKind
from iphone_backup_viewer.ui.photo_grid import MIN_TILE, PixmapLRU, StateRole, ThumbnailLoader, ThumbState


def _row(model, pred):
    return next(r for r in range(model.rowCount()) if pred(model.item(r)))


def test_thumbnails_arrive_asynchronously(make_window, services_for, qtbot, wait_grid):
    gate = threading.Event()
    fake = services_for(gate=gate)
    w = make_window(fake.services)
    wait_grid(w)
    model = w.grid.model
    idx = model.index(0)
    placeholder = idx.data(Qt.ItemDataRole.DecorationRole)
    assert isinstance(placeholder, QPixmap) and placeholder.width() == 64
    qtbot.waitUntil(lambda: len(fake.cache.calls) > 0)
    assert idx.data(StateRole) == ThumbState.LOADING  # the worker is still held at the gate
    gate.set()
    qtbot.waitUntil(lambda: idx.data(StateRole) == ThumbState.READY, timeout=5000)
    pm = idx.data(Qt.ItemDataRole.DecorationRole)
    assert pm is not placeholder
    assert pm.width() == pm.height() >= MIN_TILE - 2  # square-cropped to the tile size at request time


def test_repeated_requests_are_not_duplicated(make_window, services_for, qtbot, wait_grid):
    gate = threading.Event()
    fake = services_for(gate=gate)
    w = make_window(fake.services)
    wait_grid(w)
    idx = w.grid.model.index(0)
    key = w.grid.model.item(0).entry.file_id
    for _ in range(25):
        idx.data(Qt.ItemDataRole.DecorationRole)
    qtbot.wait(50)
    gate.set()
    qtbot.waitUntil(lambda: idx.data(StateRole) == ThumbState.READY)
    qtbot.waitUntil(lambda: w.grid.loader.pending() == 0, timeout=10000)
    assert fake.cache.calls.count(key) == 1
    assert len(fake.cache.calls) == len(set(fake.cache.calls))


def test_missing_file_gets_missing_placeholder(make_window, fake, library, qtbot, wait_grid):
    w = make_window(fake.services)
    wait_grid(w)
    model = w.grid.model
    row = _row(model, lambda i: i.entry.file_id in library.missing)
    idx = model.index(row)
    idx.data(Qt.ItemDataRole.DecorationRole)
    qtbot.waitUntil(lambda: idx.data(StateRole) == ThumbState.MISSING)
    assert "Not in this backup" in idx.data(Qt.ItemDataRole.ToolTipRole)


def test_newer_requests_run_first_and_prune_drops_offscreen(qtbot, library, services_for):
    gate = threading.Event()
    fake = services_for(gate=gate)
    cache = fake.cache
    backup = fake.services.open_backup(library.data_dir)
    loader = ThumbnailLoader(threads=1)
    loader.reset(backup, cache)
    photos = [i.entry for i in library.items if i.kind == MediaKind.PHOTO and i.entry.file_id not in library.missing]
    first, older, newer = photos[0], photos[1:4], photos[4:7]
    try:
        loader.request(first.file_id, first)  # starts at once and blocks at the gate
        qtbot.waitUntil(lambda: len(cache.calls) == 1)
        for e in older:
            loader.request(e.file_id, e)
        qtbot.wait(20)  # next event-loop pass = a new, higher priority epoch
        for e in newer:
            loader.request(e.file_id, e)
        dropped = loader.prune(keep={e.file_id for e in newer} | {older[0].file_id})
        assert dropped == 2
        gate.set()
        qtbot.waitUntil(lambda: loader.pending() == 0, timeout=10000)
        assert cache.calls == [first.file_id] + [e.file_id for e in newer] + [older[0].file_id]
    finally:
        gate.set()
        loader.shutdown()


def test_lru_is_bounded_by_bytes():
    lru = PixmapLRU(budget_bytes=3 * 10 * 10 * 4)
    for k in "abcde":
        pm = QPixmap(10, 10)
        lru.put(k, pm)
        if k == "c":
            assert lru.get("a") is not None  # touch "a" so "b" is the oldest
    assert len(lru) == 3
    assert "a" in lru and "b" not in lru and "c" not in lru
    assert lru.used <= lru.budget


def test_tiles_fill_the_row_and_visible_rows_match_layout(make_window, fake, wait_grid):
    w = make_window(fake.services)
    wait_grid(w)
    view = w.grid.view
    tile = view.tile_size()
    cols = view.columns()
    assert tile >= MIN_TILE and cols == view.layout_width() // MIN_TILE
    assert cols * tile <= view.layout_width() < cols * (tile + 1)  # tiles stretch to fill the row
    assert view.visualRect(w.grid.model.index(cols - 1)).top() == 0  # ...and the last one did not wrap
    assert view.visualRect(w.grid.model.index(cols)).left() == 0
    rows = view.visible_rows()
    assert rows.start == 0
    assert view.indexAt(QPoint(5, 5)).row() == 0
    last_visible = view.indexAt(QPoint(cols * tile - 5, view.viewport().height() - 5)).row()
    assert last_visible in rows


def test_sort_toggle_reverses_and_keeps_selection(make_window, fake, library, wait_grid):
    w = make_window(fake.services)
    wait_grid(w)
    grid = w.grid
    oldest = library.items[0]
    grid.view.selectionModel().select(grid.model.index(0), QItemSelectionModel.SelectionFlag.ClearAndSelect)
    assert grid.sort_button.text().endswith("Oldest first")
    grid.sort_button.click()
    assert grid.model.newest_first
    assert grid.model.item(0) == library.items[-1]
    assert grid.sort_button.text().endswith("Newest first")
    assert grid.selected_items() == [oldest]
    assert grid.model.row_of(oldest.entry.file_id) == len(library.items) - 1


def test_selection_count_and_export_button(make_window, fake, wait_grid):
    w = make_window(fake.services)
    wait_grid(w)
    grid = w.grid
    assert not grid.export_button.isEnabled()
    sel = grid.view.selectionModel()
    for r in (0, 3, 4):
        sel.select(grid.model.index(r), QItemSelectionModel.SelectionFlag.Select)
    assert grid.export_button.isEnabled()
    assert grid.export_button.text() == "Export 3…"
    assert "3 selected" in w.count_label.text()
    assert w.export_action.isEnabled()


def test_context_menu_offers_export(make_window, fake, qtbot, wait_grid):
    w = make_window(fake.services)
    wait_grid(w)
    grid = w.grid
    pos = grid.view.visualRect(grid.model.index(2)).center()
    grid._context_menu(pos)
    menu = grid._last_menu
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts == ["Open", "Export 1 item…", "Select All"]
    assert grid.selected_items() == [grid.model.item(2)]
    menu.close()


def test_badges_and_placeholders_paint(make_window, fake, library, qtbot, wait_grid):
    """Smoke test: every tile state and badge paints without raising."""
    w = make_window(fake.services)
    wait_grid(w)
    qtbot.waitUntil(lambda: w.grid.loader.pending() == 0, timeout=10000)
    w.grid.view.selectAll()
    image = w.grid.view.viewport().grab().toImage()
    assert not image.isNull()
    kinds = {w.grid.model.item(r).kind for r in w.grid.view.visible_rows()}
    assert MediaKind.VIDEO in kinds
