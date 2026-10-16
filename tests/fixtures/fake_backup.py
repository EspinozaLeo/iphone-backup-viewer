"""Synthetic iPhone backups for tests and demos. Everything is generated: no real backup data, ever.

    python -m tests.fixtures.fake_backup <outdir>

writes three backups into <outdir>:

- an iOS 17-style backup: Manifest.db (SQLite + NSKeyedArchiver blobs), data files under xx/xxxx…
- an iOS 7-style backup: binary Manifest.mbdb, flat data files
- an encrypted stub: IsEncrypted true and a Manifest.db that is ciphertext-like noise

The writers deliberately do not import iphone_backup_viewer. They encode the formats from the spec,
so the readers are tested against an independent implementation rather than against themselves.
"""

from __future__ import annotations

import hashlib
import io
import plistlib
import random
import sqlite3
import struct
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from plistlib import UID

from PIL import Image, ImageDraw, ImageFont, ImageOps

UTC = timezone.utc

MODERN_UDID = "00008030-001A2B3C4D5E6F70"
LEGACY_UDID = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
ENCRYPTED_UDID = "00008120-000C1D2E3F405162"

FILE, DIRECTORY, SYMLINK = "file", "directory", "symlink"
_MODES = {FILE: 0o100644, DIRECTORY: 0o040755, SYMLINK: 0o120755}
_FLAGS = {FILE: 1, DIRECTORY: 2, SYMLINK: 4}

MBDB_HEADER = b"mbdb\x05\x00"

MANIFEST_DB_SCHEMA = """
CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB);
CREATE INDEX FilesDomainIdx ON Files(domain);
CREATE INDEX FilesRelativePathIdx ON Files(relativePath);
CREATE INDEX FilesFlagsIdx ON Files(flags);
CREATE TABLE Properties (key TEXT PRIMARY KEY, value BLOB);
"""

# Camera roll, oldest first, as extractors.photos.camera_roll should return it. IMG_0008 is an
# imported old photo: oldest by date but last by name, so path order alone gets this wrong.
EXPECTED_CAMERA_ROLL = [
    "IMG_0008.JPG", "IMG_0001.JPG", "IMG_0002.JPG", "IMG_0003.JPG", "IMG_0004.JPG", "IMG_0005.JPG",
    "IMG_0006.JPG", "IMG_0007.JPG", "IMG_0009.PNG", "IMG_0010.JPG", "IMG_0011.MOV", "IMG_0012.JPG",
    "IMG_0013.JPG",
]
ROTATED_PHOTO = "Media/DCIM/100APPLE/IMG_0004.JPG"  # stored sideways, EXIF orientation 6
SCREENSHOT = "Media/DCIM/101APPLE/IMG_0009.PNG"
LIVE_PHOTO = "Media/DCIM/101APPLE/IMG_0010.JPG"
LIVE_VIDEO = "Media/DCIM/101APPLE/IMG_0010.MOV"
PORTRAIT_VIDEO = "Media/DCIM/101APPLE/IMG_0011.MOV"  # display matrix rotation -90, like an iPhone
MISSING_PHOTO = "Media/DCIM/101APPLE/IMG_0012.JPG"  # in the manifest, no data file
CORRUPT_PHOTO = "Media/DCIM/101APPLE/IMG_0013.JPG"  # data file is garbage
SIDECAR = "Media/DCIM/100APPLE/IMG_0004.AAE"
SMS_DB = ("HomeDomain", "Library/SMS/sms.db")

PHOTO_SIZE = (480, 360)  # landscape stills; the rotated one is portrait once upright
BAND_FRACTION = 0.14  # every synthetic still and video frame has a white band along its upright top


# --------------------------------------------------------------------------------------------
# Manifest records


@dataclass(frozen=True)
class FakeFile:
    domain: str
    relative_path: str
    kind: str = FILE
    data: bytes = b""
    modified: datetime = datetime(2020, 1, 1, tzinfo=UTC)
    protection_class: int = 3
    link_target: str | None = None
    store_data: bool = True  # False: listed in the manifest, data file absent from the backup
    encryption_key: bytes | None = None
    properties: tuple[tuple[bytes, bytes], ...] = ()

    @property
    def file_id(self) -> str:
        return hashlib.sha1(f"{self.domain}-{self.relative_path}".encode()).hexdigest()

    @property
    def size(self) -> int:
        return len(self.data) if self.kind == FILE else 0

    @property
    def mode(self) -> int:
        return _MODES[self.kind]

    @property
    def mtime(self) -> int:
        return int(self.modified.timestamp())


def mbfile_blob(
    relative_path: str,
    *,
    size: int,
    mtime: int,
    mode: int = _MODES[FILE],
    inode: int = 1,
    protection_class: int = 3,
    encryption_key: bytes | None = None,
    target: str | None = None,
) -> bytes:
    """The NSKeyedArchiver binary plist that Manifest.db keeps in Files.file."""
    objects: list = ["$null"]

    def add(obj) -> UID:
        objects.append(obj)
        return UID(len(objects) - 1)

    mbfile: dict = {}
    root = add(mbfile)
    mbfile.update(
        {
            "LastModified": mtime,
            "LastStatusChange": mtime,
            "Birth": mtime,
            "Size": size,
            "InodeNumber": inode,
            "UserID": 501,
            "GroupID": 501,
            "Mode": mode,
            "ProtectionClass": protection_class,
            "Flags": 0,
            "RelativePath": add(relative_path),
        }
    )
    if target is not None:
        mbfile["Target"] = add(target)
    if encryption_key is not None:
        data_class = add({"$classname": "NSMutableData", "$classes": ["NSMutableData", "NSData", "NSObject"]})
        mbfile["EncryptionKey"] = add({"NS.data": encryption_key, "$class": data_class})
    mbfile["$class"] = add({"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]})
    archive = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": root}, "$objects": objects}
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY, sort_keys=False)


def manifest_db_row(f: FakeFile, inode: int = 1) -> tuple:
    blob = mbfile_blob(
        f.relative_path,
        size=f.size,
        mtime=f.mtime,
        mode=f.mode,
        inode=inode,
        protection_class=f.protection_class,
        encryption_key=f.encryption_key,
        target=f.link_target,
    )
    return (f.file_id, f.domain, f.relative_path, _FLAGS[f.kind], blob)


def write_manifest_db(path: Path, rows: Iterable[tuple]) -> None:
    """Manifest.db with the real schema. Rows are (fileID, domain, relativePath, flags, file)."""
    path = Path(path)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(MANIFEST_DB_SCHEMA)
        conn.executemany("INSERT INTO Files VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def mbdb_string(value: bytes | str | None) -> bytes:
    if value is None:
        return b"\xff\xff"
    raw = value.encode() if isinstance(value, str) else value
    return struct.pack(">H", len(raw)) + raw


def mbdb_record(
    domain: bytes | str,
    path: bytes | str,
    *,
    mode: int,
    size: int,
    mtime: int,
    inode: int = 1,
    protection_class: int = 3,
    link_target: bytes | str | None = None,
    data_hash: bytes | None = None,
    encryption_key: bytes | None = None,
    properties: Sequence[tuple[bytes | str, bytes | str]] = (),
) -> bytes:
    parts = [
        mbdb_string(domain),
        mbdb_string(path),
        mbdb_string(link_target),
        mbdb_string(data_hash),
        mbdb_string(encryption_key),
        struct.pack(">HQIIIIIQBB", mode, inode, 501, 501, mtime, mtime, mtime, size, protection_class, len(properties)),
    ]
    for name, value in properties:
        parts += [mbdb_string(name), mbdb_string(value)]
    return b"".join(parts)


def mbdb_bytes(files: Iterable[FakeFile]) -> bytes:
    records = []
    for inode, f in enumerate(files, start=1000):
        records.append(
            mbdb_record(
                f.domain,
                f.relative_path,
                mode=f.mode,
                size=f.size,
                mtime=f.mtime,
                inode=inode,
                protection_class=f.protection_class,
                link_target=f.link_target,
                data_hash=hashlib.sha1(f.data).digest() if f.kind == FILE else None,
                encryption_key=f.encryption_key,
                properties=f.properties,
            )
        )
    return MBDB_HEADER + b"".join(records)


# --------------------------------------------------------------------------------------------
# Media


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _centered_text(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, size: int, fill) -> None:
    font = _font(size)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    x = box[0] + (box[2] - box[0] - (right - left)) // 2 - left
    y = box[1] + (box[3] - box[1] - (bottom - top)) // 2 - top
    draw.text((x, y), text, font=font, fill=fill)


_PALETTES = [
    ((20, 40, 110), (90, 160, 230)),
    ((110, 30, 60), (240, 140, 90)),
    ((20, 90, 60), (140, 220, 120)),
    ((70, 30, 110), (200, 130, 230)),
    ((100, 70, 10), (240, 200, 80)),
    ((10, 80, 100), (100, 220, 210)),
]


def _card(size: tuple[int, int], title: str, palette_index: int) -> Image.Image:
    """A gradient picture with a white band on top and a big centred title."""
    width, height = size
    top, bottom = _PALETTES[palette_index % len(_PALETTES)]
    image = ImageOps.colorize(Image.linear_gradient("L").resize(size), top, bottom)
    draw = ImageDraw.Draw(image)
    band = int(height * BAND_FRACTION)
    draw.rectangle((0, 0, width, band), fill=(255, 255, 255))
    _centered_text(draw, (0, 0, width, band), "synthetic backup", max(10, band // 2), (60, 60, 60))
    _centered_text(draw, (0, band, width, height), title, max(14, width // 11), (255, 255, 255))
    return image


def jpeg_bytes(title: str, taken: datetime, *, palette: int = 0, orientation: int = 1, size=PHOTO_SIZE) -> bytes:
    """A camera-like JPEG. With orientation 6 the pixels are stored sideways, as a phone held upright does."""
    upright = _card(size if orientation == 1 else (size[1], size[0]), title, palette)
    stored = upright.rotate(90, expand=True) if orientation == 6 else upright  # 6: viewer turns it 90° CW
    exif = Image.Exif()
    exif[0x010F] = "Apple"  # Make
    exif[0x0110] = "iPhone (synthetic)"  # Model
    exif[0x0132] = taken.strftime("%Y:%m:%d %H:%M:%S")  # DateTime
    if orientation != 1:
        exif[0x0112] = orientation
    buffer = io.BytesIO()
    stored.save(buffer, "JPEG", quality=82, exif=exif.tobytes())
    return buffer.getvalue()


def png_screenshot_bytes(title: str) -> bytes:
    width, height = 300, 650
    image = Image.new("RGBA", (width, height), (24, 26, 32, 255))
    draw = ImageDraw.Draw(image)
    band = int(height * 0.06)
    draw.rectangle((0, 0, width, band), fill=(255, 255, 255, 255))
    _centered_text(draw, (0, 0, width, band), "9:41", band // 2, (0, 0, 0))
    rng = random.Random(title)
    for row in range(4):
        for col in range(4):
            x, y = 22 + col * 68, 70 + row * 84
            color = tuple(rng.randrange(60, 250) for _ in range(3)) + (255,)
            draw.rounded_rectangle((x, y, x + 54, y + 54), radius=12, fill=color)
    _centered_text(draw, (0, 420, width, 520), title, 26, (255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


_VIDEO_CODECS = ("libx264", "h264", "mpeg4", "mjpeg")  # first one the bundled FFmpeg can encode wins


def mov_bytes(
    title: str,
    *,
    upright_size: tuple[int, int] = (320, 180),
    seconds: float = 2.0,
    fps: int = 10,
    rotation: int = 0,
) -> bytes:
    """A real QuickTime file. Blue frames before 1 s, green from 1 s, white band on the upright top.

    `rotation` is the display-matrix angle as PyAV reports it (counter-clockwise degrees): an iPhone
    held upright records sideways frames and stores -90, which is what rotation=-90 reproduces.
    """
    import av

    frames = []
    for index in range(int(seconds * fps)):
        t = index / fps
        palette = 0 if t < 1.0 else 2  # blue, then green
        upright = _card(upright_size, f"{title} {t:.1f}s", palette)
        frames.append(upright.rotate(-rotation, expand=True) if rotation else upright)

    last_error: Exception | None = None
    for codec in _VIDEO_CODECS:
        try:
            buffer = io.BytesIO()
            with av.open(buffer, mode="w", format="mov") as container:
                stream = container.add_stream(codec, rate=fps)
                stream.width, stream.height = frames[0].size
                stream.pix_fmt = "yuvj420p" if codec == "mjpeg" else "yuv420p"
                if rotation:
                    stream.set_display_rotation(rotation)
                for picture in frames:
                    for packet in stream.encode(av.VideoFrame.from_image(picture)):
                        container.mux(packet)
                for packet in stream.encode():
                    container.mux(packet)
            return buffer.getvalue()
        except Exception as exc:  # encoder missing from this FFmpeg build: try the next
            last_error = exc
    raise RuntimeError(f"No usable video encoder in this PyAV build: {last_error}")


def corrupt_jpeg_bytes() -> bytes:
    rng = random.Random(13)
    return b"\xff\xd8\xff\xe0" + bytes(rng.randrange(256) for _ in range(2048))


def sms_db_bytes() -> bytes:
    """A tiny, real, empty-ish sms.db; iteration 3 will want something here."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, date INTEGER, is_from_me INTEGER);"
            "CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);"
        )
        conn.execute("INSERT INTO handle (id) VALUES ('+15555550100')")
        conn.execute("INSERT INTO message (text, date, is_from_me) VALUES ('synthetic message', 0, 1)")
        conn.commit()
        return conn.serialize()
    finally:
        conn.close()


def aae_bytes() -> bytes:
    return plistlib.dumps(
        {
            "adjustmentBaseVersion": 0,
            "adjustmentData": b"synthetic adjustment data",
            "adjustmentFormatIdentifier": "com.apple.photo",
            "adjustmentFormatVersion": "1.4",
        },
        fmt=plistlib.FMT_XML,
    )


# --------------------------------------------------------------------------------------------
# Backups


@dataclass(frozen=True)
class BackupSpec:
    udid: str
    device_name: str
    product_type: str
    ios_version: str
    build_version: str
    last_backup: datetime
    manifest_format: str  # "sqlite" | "mbdb"
    manifest_version: str
    status_version: str
    photo_dates: tuple[datetime, ...]  # IMG_0001 … IMG_0013
    encrypted: bool = False


def _dates(*ymd: tuple[int, int, int]) -> tuple[datetime, ...]:
    return tuple(datetime(y, m, d, 14, 30, tzinfo=UTC) for y, m, d in ymd)


MODERN = BackupSpec(
    udid=MODERN_UDID,
    device_name="Synthetic iPhone 15",
    product_type="iPhone15,4",
    ios_version="17.5.1",
    build_version="21F90",
    last_backup=datetime(2025, 6, 1, 18, 45, 12, tzinfo=UTC),
    manifest_format="sqlite",
    manifest_version="10.0",
    status_version="3.3",
    photo_dates=_dates(
        (2012, 3, 14), (2013, 7, 4), (2014, 12, 25), (2015, 8, 9), (2017, 5, 20), (2019, 10, 31),
        (2022, 2, 2), (2010, 6, 1), (2023, 4, 18), (2024, 7, 21), (2024, 9, 5), (2025, 1, 11),
        (2025, 3, 3),
    ),
)

LEGACY = BackupSpec(
    udid=LEGACY_UDID,
    device_name="Synthetic iPhone 5",
    product_type="iPhone5,2",
    ios_version="7.1.2",
    build_version="11D257",
    last_backup=datetime(2014, 9, 1, 9, 15, 0, tzinfo=UTC),
    manifest_format="mbdb",
    manifest_version="9.1",
    status_version="2.4",
    photo_dates=_dates(
        (2010, 5, 2), (2010, 11, 20), (2011, 4, 9), (2011, 9, 17), (2012, 6, 30), (2013, 1, 5),
        (2013, 8, 12), (2009, 2, 14), (2013, 12, 1), (2014, 3, 10), (2014, 5, 22), (2014, 7, 4),
        (2014, 8, 1),
    ),
)

ENCRYPTED = BackupSpec(
    udid=ENCRYPTED_UDID,
    device_name="Synthetic Locked iPhone",
    product_type="iPhone17,1",
    ios_version="26.3",
    build_version="23D127",
    last_backup=datetime(2026, 2, 14, 21, 0, 0, tzinfo=UTC),
    manifest_format="sqlite",
    manifest_version="10.0",
    status_version="3.3",
    photo_dates=(),
    encrypted=True,
)


def _camera_roll_files(spec: BackupSpec) -> list[FakeFile]:
    d = spec.photo_dates
    roll = "CameraRollDomain"
    a, b = "Media/DCIM/100APPLE", "Media/DCIM/101APPLE"
    files = [
        FakeFile(roll, "", DIRECTORY, modified=d[0]),
        FakeFile(roll, "Media", DIRECTORY, modified=d[0]),
        FakeFile(roll, "Media/DCIM", DIRECTORY, modified=d[0]),
        FakeFile(roll, a, DIRECTORY, modified=d[0]),
        FakeFile(roll, b, DIRECTORY, modified=d[4]),
    ]
    for number in range(1, 9):
        folder = a if number <= 4 else b
        taken = d[number - 1]
        name = f"IMG_{number:04d}"
        orientation = 6 if number == 4 else 1
        data = jpeg_bytes(f"{taken.year} · {name}", taken, palette=number, orientation=orientation)
        files.append(FakeFile(roll, f"{folder}/{name}.JPG", data=data, modified=taken))
    files += [
        FakeFile(roll, SIDECAR, data=aae_bytes(), modified=d[3] + timedelta(days=1)),
        FakeFile(roll, SCREENSHOT, data=png_screenshot_bytes(f"Screenshot {d[8].year}"), modified=d[8]),
        FakeFile(roll, LIVE_PHOTO, data=jpeg_bytes(f"{d[9].year} · Live", d[9], palette=5), modified=d[9]),
        FakeFile(
            roll,
            LIVE_VIDEO,
            data=mov_bytes("Live", upright_size=(320, 240), seconds=1.5),
            modified=d[9],
        ),
        FakeFile(roll, PORTRAIT_VIDEO, data=mov_bytes("Video", upright_size=(180, 320), rotation=-90), modified=d[10]),
        FakeFile(
            roll,
            MISSING_PHOTO,
            data=jpeg_bytes(f"{d[11].year} · missing", d[11]),
            modified=d[11],
            store_data=False,
        ),
        FakeFile(roll, CORRUPT_PHOTO, data=corrupt_jpeg_bytes(), modified=d[12]),
        # An edited render outside DCIM: a JPEG that must not show up in the camera roll.
        FakeFile(
            roll,
            "Media/PhotoData/Mutations/DCIM/100APPLE/IMG_0004/Adjustments/FullSizeRender.jpg",
            data=jpeg_bytes("edit", d[3]),
            modified=d[3] + timedelta(days=1),
        ),
    ]
    return files


def _home_files(spec: BackupSpec) -> list[FakeFile]:
    when = spec.last_backup - timedelta(days=2)
    home = "HomeDomain"
    return [
        FakeFile(home, "", DIRECTORY, modified=when),
        FakeFile(home, "Library", DIRECTORY, modified=when),
        FakeFile(home, "Library/SMS", DIRECTORY, modified=when),
        FakeFile(home, SMS_DB[1], data=sms_db_bytes(), modified=when, protection_class=4),
        FakeFile(home, "Library/SMS/Drafts", SYMLINK, modified=when, link_target="/var/mobile/Library/SMS/Drafts"),
        FakeFile(
            home,
            "Library/Preferences/com.apple.springboard.plist",
            data=plistlib.dumps({"synthetic": True}, fmt=plistlib.FMT_BINARY),
            modified=when,
            properties=((b"com.apple.backup.excluded", b"0"),),
        ),
    ]


def backup_files(spec: BackupSpec) -> list[FakeFile]:
    files = _camera_roll_files(spec) + _home_files(spec)
    if spec.manifest_format == "mbdb":
        # Some very old backups also list the camera roll under MediaDomain; the viewer must not
        # show the photo twice.
        first = next(f for f in files if f.relative_path == "Media/DCIM/100APPLE/IMG_0001.JPG")
        files.append(FakeFile("MediaDomain", first.relative_path, data=first.data, modified=first.modified))
    # Shuffle deterministically so nothing relies on manifest order.
    random.Random(spec.udid).shuffle(files)
    return files


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _write_plists(backup: Path, spec: BackupSpec) -> None:
    when = _naive_utc(spec.last_backup)
    info = {
        "Build Version": spec.build_version,
        "Device Name": spec.device_name,
        "Display Name": spec.device_name,
        "GUID": hashlib.md5(spec.udid.encode()).hexdigest().upper(),
        "Installed Applications": [],
        "Last Backup Date": when,
        "Product Name": "iPhone",
        "Product Type": spec.product_type,
        "Product Version": spec.ios_version,
        "Serial Number": "SYNTH" + spec.udid[-7:].upper(),
        "Target Identifier": spec.udid.lower(),
        "Target Type": "Device",
        "Unique Identifier": spec.udid.upper(),
        "iTunes Settings": {},
        "iTunes Version": "12.13.2.3",
    }
    (backup / "Info.plist").write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML))

    manifest = {
        "BackupKeyBag": b"VERS\x00\x00\x00\x04\x00\x00\x00\x04" + hashlib.sha256(spec.udid.encode()).digest(),
        "Date": when,
        "IsEncrypted": spec.encrypted,
        "Lockdown": {
            "BuildVersion": spec.build_version,
            "DeviceName": spec.device_name,
            "ProductType": spec.product_type,
            "ProductVersion": spec.ios_version,
            "SerialNumber": info["Serial Number"],
            "UniqueDeviceID": spec.udid.lower(),
        },
        "SystemDomainsVersion": "24.0",
        "Version": spec.manifest_version,
        "WasPasscodeSet": True,
        "Applications": {},
    }
    if spec.encrypted:
        manifest["ManifestKey"] = b"\x04\x00\x00\x00" + hashlib.sha256(b"manifest key" + spec.udid.encode()).digest()
    (backup / "Manifest.plist").write_bytes(plistlib.dumps(manifest, fmt=plistlib.FMT_BINARY))

    status = {
        "BackupState": "new",
        "Date": when,
        "IsFullBackup": False,
        "SnapshotState": "finished",
        "UUID": hashlib.md5(b"status" + spec.udid.encode()).hexdigest().upper(),
        "Version": spec.status_version,
    }
    (backup / "Status.plist").write_bytes(plistlib.dumps(status, fmt=plistlib.FMT_BINARY))


def data_file_path(backup: Path, f: FakeFile, manifest_format: str) -> Path:
    if manifest_format == "sqlite":
        return backup / f.file_id[:2] / f.file_id
    return backup / f.file_id


def build_backup(root: Path, spec: BackupSpec, files: Sequence[FakeFile]) -> Path:
    backup = Path(root) / spec.udid
    backup.mkdir(parents=True, exist_ok=True)
    for f in files:
        if f.kind == FILE and f.store_data:
            dest = data_file_path(backup, f, spec.manifest_format)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f.data)
    if spec.manifest_format == "sqlite":
        write_manifest_db(backup / "Manifest.db", (manifest_db_row(f, inode) for inode, f in enumerate(files, 1000)))
    else:
        (backup / "Manifest.mbdb").write_bytes(mbdb_bytes(files))
    _write_plists(backup, spec)
    return backup


def make_modern_backup(root: Path) -> Path:
    """iOS 17-style backup in root/<udid>: Manifest.db, files under 2-character folders."""
    return build_backup(root, MODERN, backup_files(MODERN))


def make_legacy_backup(root: Path) -> Path:
    """iOS 7-style backup in root/<udid>: binary Manifest.mbdb, flat file layout."""
    return build_backup(root, LEGACY, backup_files(LEGACY))


def make_encrypted_backup_stub(root: Path) -> Path:
    """IsEncrypted backup in root/<udid>. Its Manifest.db is noise, as a real encrypted one looks."""
    backup = Path(root) / ENCRYPTED.udid
    backup.mkdir(parents=True, exist_ok=True)
    rng = random.Random(ENCRYPTED.udid)
    (backup / "Manifest.db").write_bytes(bytes(rng.randrange(256) for _ in range(8192)))
    for _ in range(3):
        file_id = hashlib.sha1(str(rng.random()).encode()).hexdigest()
        dest = backup / file_id[:2] / file_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(bytes(rng.randrange(256) for _ in range(1024)))
    _write_plists(backup, ENCRYPTED)
    return backup


def make_all(root: Path) -> list[Path]:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    return [make_modern_backup(root), make_legacy_backup(root), make_encrypted_backup_stub(root)]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m tests.fixtures.fake_backup <outdir>", file=sys.stderr)
        return 2
    for path in make_all(Path(args[0])):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
