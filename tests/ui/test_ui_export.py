import os
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QMessageBox

from iphone_backup_viewer.ui.export import export_items, reserve_names, safe_filename


def _items_named(library, *rels):
    by_rel = {i.entry.relative_path: i for i in library.items}
    return [by_rel[r] for r in rels]


def _mtime_ok(path: Path, dt) -> bool:
    return abs(path.stat().st_mtime - dt.timestamp()) < 0.01


def test_export_keeps_names_adds_collision_suffixes_and_mtimes(library, fake, tmp_path):
    backup = fake.services.open_backup(library.data_dir)
    a, b = _items_named(library, "Media/DCIM/100APPLE/IMG_0001.JPG", "Media/DCIM/101APPLE/IMG_0001.JPG")
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "IMG_0001.JPG").write_bytes(b"already here")  # from an earlier export
    result = export_items(backup, [a, b], dest)
    assert not result.cancelled and not result.skipped
    assert [p.name for p in result.copied] == ["IMG_0001 (1).JPG", "IMG_0001 (2).JPG"]
    assert (dest / "IMG_0001.JPG").read_bytes() == b"already here"  # never overwritten
    assert (dest / "IMG_0001 (1).JPG").read_bytes() == backup.local_path(a.entry).read_bytes()
    assert (dest / "IMG_0001 (2).JPG").read_bytes() == backup.local_path(b.entry).read_bytes()
    assert _mtime_ok(dest / "IMG_0001 (1).JPG", a.entry.modified)
    assert _mtime_ok(dest / "IMG_0001 (2).JPG", b.entry.modified)


def test_live_photo_exports_its_mov_with_a_matching_suffix(library, fake, tmp_path):
    backup = fake.services.open_backup(library.data_dir)
    live = next(i for i in library.items if i.live_video is not None)
    still, motion = live.entry.name, live.live_video.name
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / still).write_bytes(b"x")  # only the still collides...
    result = export_items(backup, [live], dest)
    stem = os.path.splitext(still)[0]
    # ...but both get "(1)", so the pair still matches by name
    assert [p.name for p in result.copied] == [f"{stem} (1).JPG", f"{stem} (1).MOV"]
    assert motion.endswith(".MOV")
    assert _mtime_ok(result.copied[1], live.live_video.modified)


def test_missing_files_are_skipped_and_reported(library, fake, tmp_path):
    backup = fake.services.open_backup(library.data_dir)
    missing = [i for i in library.items if i.entry.file_id in library.missing]
    present = library.items[0]
    result = export_items(backup, [present, *missing], tmp_path / "out")
    assert len(result.copied) == 1
    assert [name for name, _ in result.skipped] == [i.entry.name for i in missing]
    assert all(reason == "not in this backup" for _, reason in result.skipped)
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [present.entry.name]


def test_cancel_stops_and_leaves_no_partial_files(library, fake, tmp_path):
    backup = fake.services.open_backup(library.data_dir)
    items = [i for i in library.items if i.entry.file_id not in library.missing][:6]
    seen = []
    result = export_items(backup, items, tmp_path / "out", progress=lambda d, t, n: seen.append(d), cancelled=lambda: len(seen) >= 2)
    assert result.cancelled
    assert len(result.copied) == 2
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(p.name for p in result.copied)


def test_safe_filename_blocks_traversal_and_reserved_names():
    assert safe_filename("..\\..\\evil.jpg", "id") == ".._.._evil.jpg"
    assert safe_filename("a:b?.JPG", "id") == "a_b_.JPG"
    assert safe_filename("CON.jpg", "id") == "_CON.jpg"
    assert safe_filename("...", "fallbackid") == "fallbackid"
    assert safe_filename("IMG_0001.HEIC", "id") == "IMG_0001.HEIC"


def test_reserve_names_skips_folders_and_existing_files(tmp_path):
    (tmp_path / "IMG_1.JPG").mkdir()
    (tmp_path / "IMG_1 (1).MOV").write_bytes(b"")
    paths = reserve_names(tmp_path, ["IMG_1.JPG", "IMG_1.MOV"])
    assert [p.name for p in paths] == ["IMG_1 (2).JPG", "IMG_1 (2).MOV"]
    assert all(p.is_file() for p in paths)


def test_export_from_grid_runs_in_worker_and_reports(make_window, fake, library, qtbot, tmp_path, wait_grid):
    w = make_window(fake.services)
    wait_grid(w)
    grid = w.grid
    live_row = next(r for r in range(grid.model.rowCount()) if grid.model.item(r).live_video is not None)
    missing_row = next(r for r in range(grid.model.rowCount()) if grid.model.item(r).entry.file_id in library.missing)
    sel = grid.view.selectionModel()
    for r in (0, 1, live_row, missing_row):
        sel.select(grid.model.index(r), QItemSelectionModel.SelectionFlag.Select)
    dest = tmp_path / "exported"
    w._ask_directory = lambda title: str(dest)
    with qtbot.waitSignal(w.export.finished, timeout=10000) as blocker:
        grid.export_button.click()
    result = blocker.args[0]
    assert len(result.copied) == 4  # two stills + Live still + its MOV
    assert len(result.skipped) == 1
    names = sorted(p.name for p in dest.iterdir())
    assert len(names) == 4 and any(n.endswith(".MOV") for n in names)
    box = w.export.report
    assert isinstance(box, QMessageBox)
    assert "Exported 4 files" in box.text()
    assert "Skipped 1 file" in box.text()
    assert not w.export.running()
    box.close()
