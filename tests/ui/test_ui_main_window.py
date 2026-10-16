import threading

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from iphone_backup_viewer import APP_NAME
from iphone_backup_viewer.ui import theme
from iphone_backup_viewer.ui.backup_list import EncryptedRole, IncompleteRole
from iphone_backup_viewer.ui.main_window import ENCRYPTED_MESSAGE, MESSAGES_COMING


def test_window_builds_with_photos_and_disabled_messages_tab(make_window, fake):
    w = make_window(fake.services, scan_on_start=False)
    assert w.windowTitle() == APP_NAME
    assert w.tabs.tabText(w.photos_tab) == "Photos"
    assert w.tabs.isTabEnabled(w.photos_tab)
    assert w.tabs.tabText(w.messages_tab) == "Messages"
    assert not w.tabs.isTabEnabled(w.messages_tab)
    assert w.tabs.tabToolTip(w.messages_tab) == MESSAGES_COMING
    file_menu = w.menuBar().actions()[0]
    texts = [a.text().replace("&", "") for a in file_menu.menu().actions() if not a.isSeparator()]
    assert "Open folder…" in texts and "Exit" in texts


def test_theme_is_fusion_with_light_and_dark_palettes(qapp):
    theme.apply_theme(qapp)
    assert QApplication.style().name().lower() == "fusion"
    dark = theme.build_palette(True)
    light = theme.build_palette(False)
    assert dark.color(QPalette.ColorRole.Window).lightness() < 60
    assert light.color(QPalette.ColorRole.Window).lightness() > 200
    assert dark.color(QPalette.ColorRole.Text).lightness() > 200


def test_empty_state_when_no_backups_found(make_window, services_for, qtbot):
    fake = services_for(backups=[])
    w = make_window(fake.services)
    qtbot.waitUntil(lambda: w.central.currentWidget() is w.empty_page)
    assert w.empty_page.title.text() == "No iPhone backups found"
    assert w.empty_page.button.isVisible()
    assert w.empty_page.button.text() == "Open folder…"


def test_scan_failure_still_shows_empty_state(make_window, fake, qtbot):
    def boom():
        raise NotImplementedError("discovery not ready")

    fake.services.default_roots = boom
    w = make_window(fake.services)
    qtbot.waitUntil(lambda: w.central.currentWidget() is w.empty_page)
    assert "discovery not ready" in w.empty_page.note.text()


def test_sidebar_populates_from_fake_scan(make_window, fake, qtbot):
    w = make_window(fake.services)
    qtbot.waitUntil(lambda: w.sidebar.model.rowCount() == 3)
    model = w.sidebar.model
    names = [model.index(r).data() for r in range(3)]
    assert names == ["Work iPhone", "Leo's iPhone", "Old iPhone 4"]  # newest backup first
    assert model.index(0).data(EncryptedRole) is True
    assert model.index(2).data(IncompleteRole) is True
    assert model.index(1).data(IncompleteRole) is False
    tip = model.index(1).data(0x0003)  # ToolTipRole
    assert "iOS 16.4.1" in tip and "2024" in tip
    assert w.central.currentWidget() is w.splitter


def test_first_openable_backup_is_opened_and_fills_grid(make_window, fake, library, wait_grid):
    w = make_window(fake.services)
    wait_grid(w)
    assert fake.opened == [library.data_dir]
    assert w.sidebar.current() == library.info
    assert w.grid.model.rowCount() == len(library.items)
    assert w.count_label.text().startswith(f"{len(library.items)} items")


def test_encrypted_backup_is_not_opened(make_window, fake, library, qtbot, wait_grid):
    w = make_window(fake.services)
    wait_grid(w)
    fake.opened.clear()
    w.sidebar.select_row(0)  # Work iPhone, encrypted
    assert w.photos.currentWidget() is w.photo_message
    assert w.photo_message.body.text() == ENCRYPTED_MESSAGE
    qtbot.wait(100)
    assert fake.opened == []
    assert w.grid.model.rowCount() == 0
    assert w.count_label.text() == ""


def test_incomplete_backup_explains_instead_of_opening(make_window, fake, qtbot, wait_grid):
    w = make_window(fake.services)
    wait_grid(w)
    fake.opened.clear()
    w.sidebar.select_row(2)
    assert w.photos.currentWidget() is w.photo_message
    assert "incomplete" in w.photo_message.title.text()
    qtbot.wait(50)
    assert fake.opened == []


def test_busy_indicator_while_backup_opens(make_window, fake, library, qtbot):
    release = threading.Event()
    real_open = fake.services.open_backup

    def slow_open(path):
        release.wait(5)
        return real_open(path)

    fake.services.open_backup = slow_open
    w = make_window(fake.services)
    qtbot.waitUntil(lambda: w.photos.currentWidget() is w.photo_busy)
    assert w.busy.isVisible()
    assert "Opening Leo's iPhone" in w.photo_busy.title.text()
    release.set()
    qtbot.waitUntil(lambda: w.photos.currentWidget() is w.grid)
    assert not w.busy.isVisible()


def test_stale_open_result_is_dropped(make_window, fake, library, qtbot):
    """Picking another backup while one is still opening must not show the first one's photos."""
    release = threading.Event()
    real_open = fake.services.open_backup

    def slow_open(path):
        release.wait(5)
        return real_open(path)

    fake.services.open_backup = slow_open
    w = make_window(fake.services)
    qtbot.waitUntil(lambda: w.photos.currentWidget() is w.photo_busy)
    w.sidebar.select_row(0)  # encrypted: shows its message immediately
    release.set()
    qtbot.wait(200)
    assert w.photos.currentWidget() is w.photo_message
    assert w.grid.model.rowCount() == 0


def test_open_folder_adds_backups_and_opens_one(make_window, fake, library, qtbot, wait_grid):
    fake.services.default_roots = lambda: []
    w = make_window(fake.services)
    qtbot.waitUntil(lambda: w.central.currentWidget() is w.empty_page)
    w._ask_directory = lambda title: str(library.root)
    w.empty_page.button.click()
    wait_grid(w)
    assert w.sidebar.model.rowCount() == 3
    assert w.sidebar.current() == library.info


def test_open_folder_without_backups_says_so(make_window, fake, tmp_path, qtbot):
    fake.services.default_roots = lambda: []
    w = make_window(fake.services)
    qtbot.waitUntil(lambda: w.central.currentWidget() is w.empty_page)
    w._ask_directory = lambda title: str(tmp_path)
    w.open_action.trigger()
    qtbot.waitUntil(lambda: w.empty_page.note.isVisible())
    assert str(tmp_path) in w.empty_page.note.text()


def test_open_failure_is_reported(make_window, fake, library, qtbot):
    def broken(path):
        raise OSError("Manifest.db is corrupt")

    fake.services.open_backup = broken
    w = make_window(fake.services)
    qtbot.waitUntil(lambda: w.photos.currentWidget() is w.photo_message)
    assert w.photo_message.title.text() == "Couldn't open this backup"
    assert "corrupt" in w.photo_message.body.text()
