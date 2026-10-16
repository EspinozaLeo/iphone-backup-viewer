# Architecture

A read-only desktop viewer for iTunes / Finder / Apple Devices backups of an iPhone, iOS 4 through current.
It is Python + PySide6 (Qt 6) and targets Windows first.

## Principles

1. **Read-only.** Nothing is ever written inside a backup folder. Decrypted copies and thumbnails go to the per-user cache.
2. **Offline.** No network calls, no telemetry. These are people's private memories.
3. **Old backups are first-class.** iOS 4–9 (`Manifest.mbdb`) gets the same support as iOS 10+ (`Manifest.db`).
4. **One bad file never breaks a view.** Corrupt media shows a placeholder, and a missing database shows "not in this backup".
5. **UI on the main thread only.** Parsing, decryption and decoding run on worker threads.

## Layers

```
┌──────────────────────────────────────────────────────────────┐
│ ui/          PySide6 widgets, models, workers  (only Qt here)  │
├──────────────────────────────────────────────────────────────┤
│ extractors/  photos, messages, contacts, calls, notes, ...     │
│ media/       image decode (Pillow + pi-heif), thumbnails (PyAV)│
├──────────────────────────────────────────────────────────────┤
│ core/        discovery · Backup · manifest readers · crypto    │
└──────────────────────────────────────────────────────────────┘
        depends downward only; core/ and media/ never import Qt
```

| Package | Responsibility |
|---|---|
| `core/models.py` | Shared value types: `BackupInfo`, `FileEntry`, `MediaItem`. **This is the contract between layers.** |
| `core/discovery.py` | Find backup folders (default OS locations + user-picked folder) and summarise them from plists. |
| `core/backup.py` | `Backup`: opened manifest, lookups by domain/path, and plaintext access to file contents. |
| `core/manifest/` | `mbdb.py` (iOS 4–9 binary format), `sqlite.py` (iOS 10+ `Manifest.db`). |
| `core/crypto/` | *(Iteration 2)* Keybag parsing, password → class keys, per-file AES. Decrypted files are cached on disk. |
| `extractors/` | One module per data type. Each turns raw backup files into typed Python objects and never touches Qt. |
| `media/` | `open_image()` (JPEG/PNG/HEIC with EXIF orientation), `ThumbnailCache` (disk-cached JPEG thumbnails). |
| `ui/` | Main window, backup sidebar, photo grid, media viewer, export. Qt models plus `QThreadPool` workers. |

Keeping `core/`, `extractors/` and `media/` free of Qt makes them testable without a display. It also leaves room for a
CLI exporter later.

## Data flow (photos)

```
discovery.scan(root) ──► [BackupInfo…] ──► sidebar
                                    │ user selects
Backup.open(path) ──► manifest parsed once, entries cached in memory (100k entries ≈ tens of MB)
extractors.photos.camera_roll(backup) ──► [MediaItem…] ──► PhotoGridModel (rows only, no pixels)
PhotoGridModel.data(DecorationRole) ──► cache hit? pixmap : enqueue ThumbnailJob(QThreadPool)
ThumbnailCache.get() ──► Backup.local_path() ──► decode ──► JPEG in cache ──► signal ──► repaint cell
```

## Backup format essentials

See `docs/research/backup-format.md` for detail and sources.

- File ID = `SHA1("{domain}-{relativePath}")`. The data lives at `{backup}/{id[:2]}/{id}` (iOS 10+) or `{backup}/{id}` (iOS 4–9).
- The type of `Manifest.*` tells the format. Use `Status.plist` `Version` only as a hint.
- Encryption: `Manifest.plist` `IsEncrypted`. On iOS 10.2+ the `Manifest.db` itself is encrypted, and unlocking costs ~3 s of PBKDF2,
  so the derived key is cached for the session.

## Caches

`platformdirs.user_cache_dir("iPhoneBackupViewer")`, keyed by backup **snapshot**, not by phone:

```
<cache>/<snapshot>/thumbs/<file_id>_<size>.jpg
<cache>/<snapshot>/plain/<file_id><ext>        # decrypted copies (iteration 2+)

<snapshot> = core.cache_key.backup_cache_key(info)
           = <udid>-<last_backup as %Y%m%dT%H%M%SZ>      e.g. 00008030-001A…-20250601T184512Z
           = <udid>-<sha1(resolved folder path)[:12]>    when the backup date is unknown
```

Archived backups of one phone (iTunes "Archive" makes folders like `<UDID>-20120304-…`) share a UDID, and the
same file ID can hold different contents in each. A UDID-only key would show, and for `plain/` serve, the wrong
file. Two copies of the same snapshot in different places share a key, which is correct.

Writes go to a temp name followed by `os.replace`, so concurrent workers never see half-written files.

## Testing

There is no real backup in CI, and no real backup may ever be committed. `tests/fixtures/fake_backup.py` **generates**
synthetic backups in both formats (with real tiny JPEG/PNG/MOV files, a Live Photo pair, and an encrypted-flag backup).
`python -m tests.fixtures.fake_backup demo-backups/` writes them out for manual testing. UI tests use pytest-qt
with `QT_QPA_PLATFORM=offscreen`.

## Dependencies and licenses

The app is MIT.

| Package | License | Why |
|---|---|---|
| PySide6 | LGPL-3.0 | Qt widgets + Qt Multimedia (bundled FFmpeg backend plays H.264 and HEVC) |
| Pillow | MIT-CMU | Image decode/resize |
| pi-heif | BSD-3 (bundles LGPL libheif/libde265) | HEIC decode. Chosen over pillow-heif, which bundles the GPL x265 encoder |
| av (PyAV) | BSD-3 (bundles FFmpeg) | Video thumbnails. **The wheel's bundled FFmpeg is built with libx264/libx265 (GPL)**, even though it reports "LGPL version 3 or later". This must be resolved before any binary release: either grab thumbnail frames via Qt Multimedia (LGPL FFmpeg), or ship a custom LGPL-only PyAV build |
| platformdirs | MIT | Cache locations |

Planned: `cryptography` (Apache-2.0/BSD) for decryption, and `python-typedstream` (LGPL-3.0) for iOS message bodies.
GPL projects (imessage-exporter, crabapple) are reference reading only, never copied or linked.

## Iterations

| # | Delivers |
|---|---|
| 1 | Backup list (all iOS versions, encrypted ones shown locked) · photo/video gallery with thumbnails · full-size viewer + video playback · export selected originals |
| 2 | Encrypted backups: password prompt, all iOS versions |
| 3 | Messages: conversations, bubbles, inline attachments, contact names |
| 4 | File browser by domain + export anything |
| 5 | Contacts, call history, notes, voicemail, voice memos |
| 6 | WhatsApp |
| 7 | Windows installer + GitHub Actions release builds |
