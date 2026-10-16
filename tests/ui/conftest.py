"""Fakes for the UI tests: a generated camera roll plus stand-ins for core/, extractors/ and media/.

Everything here is synthetic (Pillow gradients with year labels, tiny H.264 clips from PyAV). The data
files are stored the way a backup stores them: under <id[:2]>/<id>, with no extension.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PIL import Image, ImageDraw, ImageFont, ImageOps  # noqa: E402

from iphone_backup_viewer.core.errors import BackupError, MissingFileError  # noqa: E402
from iphone_backup_viewer.core.models import (  # noqa: E402
    BackupInfo,
    FileEntry,
    FileKind,
    ManifestFormat,
    MediaItem,
    MediaKind,
)
from iphone_backup_viewer.ui.services import Services  # noqa: E402

DOMAIN = "CameraRollDomain"
UDID = "00008030-001A2B3C4D5E6F70"

# Warm, faded colour pairs: old-photo moods rather than test-card primaries.
_COLOURS = [
    ("#ff9a8b", "#ff6a88"),
    ("#fbc2eb", "#a6c1ee"),
    ("#84fab0", "#8fd3f4"),
    ("#fccb90", "#d57eeb"),
    ("#e0c3fc", "#8ec5fc"),
    ("#f6d365", "#fda085"),
    ("#a1c4fd", "#c2e9fb"),
    ("#d4fc79", "#96e6a1"),
    ("#ffecd2", "#fcb69f"),
    ("#cfd9df", "#e2ebf0"),
    ("#667eea", "#764ba2"),
    ("#f093fb", "#f5576c"),
    ("#4facfe", "#00f2fe"),
    ("#43e97b", "#38f9d7"),
    ("#fa709a", "#fee140"),
    ("#30cfd0", "#330867"),
]


def file_id(relative_path: str, domain: str = DOMAIN) -> str:
    return hashlib.sha1(f"{domain}-{relative_path}".encode()).hexdigest()


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def gradient_image(size: tuple[int, int], colours: tuple[str, str], label: str, caption: str) -> Image.Image:
    w, h = size
    a = Image.new("RGB", size, colours[0])
    b = Image.new("RGB", size, colours[1])
    mask = Image.linear_gradient("L").rotate(-35, expand=True).resize(size)
    img = Image.composite(b, a, mask)
    d = ImageDraw.Draw(img)
    big = _font(int(min(w, h) * 0.26))
    small = _font(int(min(w, h) * 0.055))
    d.text((w / 2, h / 2), label, font=big, fill=(255, 255, 255), anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0, 40))
    d.text((w / 2, h / 2 + min(w, h) * 0.2), caption, font=small, fill=(255, 255, 255), anchor="mm")
    return img


def write_clip(path: Path, colours: tuple[str, str], label: str, frames: int = 30, size: tuple[int, int] = (320, 240)) -> None:
    import av

    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w", format="mov") as container:
        stream = container.add_stream("libx264" if "libx264" in av.codecs_available else "mpeg4", rate=30)
        stream.width, stream.height, stream.pix_fmt = size[0], size[1], "yuv420p"
        for i in range(frames):
            img = gradient_image(size, colours, label, f"frame {i + 1}")
            for packet in stream.encode(av.VideoFrame.from_image(img)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


@dataclass
class FakeLibrary:
    root: Path  # "a folder containing backups"
    data_dir: Path  # the normal backup's folder (data files under id[:2]/id)
    info: BackupInfo
    encrypted: BackupInfo
    incomplete: BackupInfo
    items: list[MediaItem]
    missing: set[str] = field(default_factory=set)

    def backups(self) -> list[BackupInfo]:
        return [self.info, self.encrypted, self.incomplete]


class FakeBackup:
    def __init__(self, info: BackupInfo, data_dir: Path, missing: set[str]) -> None:
        self.info = info
        self._data_dir = data_dir
        self._missing = missing

    def local_path(self, entry: FileEntry) -> Path:
        fid = entry.file_id.split("-")[0]  # screenshot_demo --many clones entries as "<sha1>-<n>"
        path = self._data_dir / fid[:2] / fid
        if fid in self._missing or not path.exists():
            raise MissingFileError(entry.relative_path)
        return path

    def read_bytes(self, entry: FileEntry) -> bytes:
        return self.local_path(entry).read_bytes()


def _first_frame(path: Path) -> Image.Image:
    import av

    with av.open(str(path)) as container:
        for frame in container.decode(video=0):
            return frame.to_image()
    raise ValueError("no video frames")


def fake_open_image(path: Path, max_size: int | None = None) -> Image.Image:
    img = Image.open(path)
    img.load()
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max_size:
        img.thumbnail((max_size, max_size))
    return img


class FakeThumbnailCache:
    """Same contract as media.thumbnails.ThumbnailCache. `gate`, if given, holds every get() until set."""

    def __init__(self, cache_dir: Path, gate: threading.Event | None = None) -> None:
        self.cache_dir = cache_dir
        self.gate = gate
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def get(self, backup, entry: FileEntry, size: int = 256) -> Path | None:
        with self._lock:
            self.calls.append(entry.file_id)
        if self.gate is not None:
            self.gate.wait(10)
        try:
            src = backup.local_path(entry)
        except MissingFileError:
            return None
        out = self.cache_dir / backup.info.udid / "thumbs" / f"{entry.file_id}_{size}.jpg"
        if out.exists():
            return out
        try:
            img = _first_frame(src) if entry.suffix in (".mov", ".mp4") else fake_open_image(src)
        except Exception:
            return None
        img.thumbnail((size, size))
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + f".{threading.get_ident()}.tmp")
        img.save(tmp, "JPEG", quality=85)
        os.replace(tmp, out)
        return out


def build_library(dest: Path, photos: int = 40) -> FakeLibrary:
    """A fake backup folder with `photos` stills spread over 2009–2024, three videos, two Live Photos,
    two missing files, and two different photos that share the name IMG_0001.JPG."""
    root = dest / "Backup"
    backup_dir = root / UDID
    backup_dir.mkdir(parents=True, exist_ok=True)
    start = datetime(2009, 6, 14, 15, 30, tzinfo=timezone.utc)
    span = datetime(2024, 8, 2, tzinfo=timezone.utc) - start
    items: list[MediaItem] = []
    missing: set[str] = set()

    def store(rel: str, when: datetime, size: int, write) -> FileEntry:
        fid = file_id(rel)
        path = backup_dir / fid[:2] / fid
        path.parent.mkdir(parents=True, exist_ok=True)
        if write is None:
            missing.add(fid)
        else:
            write(path)
            size = path.stat().st_size
        return FileEntry(fid, DOMAIN, rel, FileKind.FILE, size, when)

    total = photos + 3
    video_slots = {photos // 4, photos // 2, (3 * photos) // 4}
    live_slots = {5, photos - 6}
    missing_slots = {9, photos - 3}
    n = 0
    for i in range(total):
        when = start + span * (i / max(1, total - 1)) + timedelta(minutes=i * 7)
        colours = _COLOURS[i % len(_COLOURS)]
        folder = 100 + i // 20
        number = 1 if i == 21 else i + 1  # i == 21 is 101APPLE/IMG_0001.JPG, clashing with 100APPLE/IMG_0001.JPG
        label = str(when.year)
        if i in video_slots:
            rel = f"Media/DCIM/{folder}APPLE/IMG_{number:04d}.MOV"
            entry = store(rel, when, 0, lambda p, c=colours, t=label: write_clip(p, c, t))
            items.append(MediaItem(entry, MediaKind.VIDEO, when))
            continue
        portrait = i % 5 == 3
        size = (720, 960) if portrait else (960, 720)
        rel = f"Media/DCIM/{folder}APPLE/IMG_{number:04d}.JPG"
        caption = f"IMG_{number:04d} · {when:%b %Y}"
        writer = None if i in missing_slots else (lambda p, s=size, c=colours, t=label, cap=caption: gradient_image(s, c, t, cap).save(p, "JPEG", quality=88))
        entry = store(rel, when, 0, writer)
        live = None
        if i in live_slots:
            live_rel = rel[:-4] + ".MOV"
            live = store(live_rel, when, 0, lambda p, c=colours, t=label: write_clip(p, c, t, frames=20))
        items.append(MediaItem(entry, MediaKind.PHOTO, when, live))
        n += 1

    info = BackupInfo(backup_dir, UDID, "Leo's iPhone", "iPhone10,3", "16.4.1", datetime(2024, 8, 3, 21, 5, tzinfo=timezone.utc), False, ManifestFormat.SQLITE)
    enc_dir = root / "ENCRYPTED0000000000000000000000000000000"
    enc_dir.mkdir(exist_ok=True)
    encrypted = BackupInfo(enc_dir, "ENCRYPTED", "Work iPhone", "iPhone14,2", "17.2", datetime(2025, 1, 9, 9, 0, tzinfo=timezone.utc), True, ManifestFormat.SQLITE)
    inc_dir = root / "INCOMPLETE000000000000000000000000000000"
    inc_dir.mkdir(exist_ok=True)
    incomplete = BackupInfo(inc_dir, "INCOMPLETE", "Old iPhone 4", "iPhone3,1", "7.1.2", datetime(2014, 3, 2, 18, 0, tzinfo=timezone.utc), False, None)
    return FakeLibrary(root, backup_dir, info, encrypted, incomplete, items, missing)


@dataclass
class FakeServices:
    library: FakeLibrary
    services: Services
    cache: FakeThumbnailCache
    opened: list[Path] = field(default_factory=list)


def make_services(library: FakeLibrary, cache_dir: Path, *, gate: threading.Event | None = None, backups: list[BackupInfo] | None = None) -> FakeServices:
    cache = FakeThumbnailCache(cache_dir, gate)
    found = library.backups() if backups is None else backups
    fake = FakeServices(library, Services(), cache)

    def scan(root: Path) -> list[BackupInfo]:
        root = Path(root)
        if root == library.root:
            return sorted(found, key=lambda b: b.last_backup, reverse=True)
        return [b for b in found if b.path == root]

    def open_backup(path: Path) -> FakeBackup:
        fake.opened.append(Path(path))
        if Path(path) != library.data_dir:
            raise BackupError(f"not a readable backup: {path}")
        return FakeBackup(library.info, library.data_dir, library.missing)

    fake.services = Services(
        default_roots=lambda: [library.root],
        scan=scan,
        open_backup=open_backup,
        camera_roll=lambda backup: list(library.items),
        open_image=fake_open_image,
        thumbnails=cache,
    )
    return fake


@pytest.fixture(scope="session")
def library(tmp_path_factory) -> FakeLibrary:
    return build_library(tmp_path_factory.mktemp("library"))


@pytest.fixture
def fake(library, tmp_path) -> FakeServices:
    return make_services(library, tmp_path / "cache")


@pytest.fixture
def make_window(qtbot):
    """Build a MainWindow over fakes; always shuts its workers down at the end of the test."""
    from iphone_backup_viewer.ui.main_window import MainWindow

    windows = []

    def _make(services: Services, **kwargs) -> MainWindow:
        window = MainWindow(services, **kwargs)
        qtbot.addWidget(window)
        window.resize(1100, 720)
        window.show()
        windows.append(window)
        return window

    yield _make
    for window in windows:
        window.shutdown()


def wait_for_grid(qtbot, window, timeout: int = 5000) -> None:
    qtbot.waitUntil(lambda: window.photos.currentWidget() is window.grid and window.grid.model.rowCount() > 0, timeout=timeout)


@pytest.fixture
def wait_grid(qtbot):
    return lambda window, timeout=5000: wait_for_grid(qtbot, window, timeout)


@pytest.fixture
def services_for(library, tmp_path):
    """make_services with the session library and a per-test cache folder."""
    return lambda **kwargs: make_services(library, tmp_path / "cache", **kwargs)
