# GUI stack options — research notes (2026-10-16)

Gathered by a research agent. UNVERIFIED marks claims not confirmed against a primary source.

| | First viewer | 100k chat / photo grid | HEIC / HEVC on Windows | Packaging | Parser reuse | Contributors |
|---|---|---|---|---|---|---|
| PySide6 (Qt) | Fastest | QListView + custom delegate or QML; bubbles hand-built | HEIC via pillow-heif. HEVC **plays with no paid extension** (Qt's bundled FFmpeg) | ~100+ MB; PyInstaller AV false positives | Python (best) | Python big, Qt smaller |
| Tauri 2 + React | Medium | Best (web virtualizers) | HEIC ✗, HEVC needs the paid MS extension, so **convert/transcode** | Small (~12 MB) + ffmpeg sidecar | Rust crates are GPL-3 | Web big, Rust smaller |
| FastAPI + web UI | Fast | Best | Same WebView2 problem as Tauri | Two runtimes + Python packaging | Python | Mixed |
| Electron | Medium | Best | HEIC ✗. HEVC via GPU hardware decode only | 80–200 MB | Weak (Node) | Largest |
| Avalonia (.NET) | Slowest | DIY scroll anchoring | Media player is a paid Pro add-on | Medium | Weak | Small |

## Media facts

- No Chrome/Edge/WebView2 decodes HEIC ([caniuse](https://caniuse.com/heif)).
- WebView2 HEVC needs the Microsoft HEVC Video Extension. Without it the failure is silent (audio plays, no picture).
  Source is one project's reports, not Microsoft docs.
- Qt Multimedia on Windows defaults to an FFmpeg 7.1 backend built without `--enable-gpl` that keeps the HEVC decoder.
  PySide6 wheels bundle it ([Qt docs](https://doc.qt.io/qt-6.10/qtmultimedia-index.html)).
- Shipping an HEVC decoder has possible patent exposure (UNVERIFIED legally).

## Signing (all stacks)

Unsigned apps get SmartScreen warnings. [SignPath Foundation](https://signpath.org/terms) gives free OSS signing but requires
OSI licenses for all components. Azure Artifact Signing is $9.99/mo and limited to individuals in the US and Canada.

## Option A — Python + PySide6

**Tools:** PySide6 6.11, pillow-heif, iphone_backup_decrypt (MIT), python-typedstream (LGPL), PyInstaller or pyside6-deploy (Nuitka).

**Costs:**
- Chat bubbles and variable-height rows at 100k scale need careful custom work.
- PyInstaller builds trip Defender ML heuristics.
- HEIC thumbnails need a worker pool and a disk cache.
- Big installer.

## Option B — Tauri 2 (Rust) + React

**Tools:** tauri 2.12, @tanstack/react-virtual (react-virtuoso's message list is paid), libheif-rs or an ffmpeg 8.1+ sidecar, tauri-action for CI.

**Costs:**
- On Windows, HEIC must be converted, and HEVC .MOV must be transcoded or the user buys the extension.
- Using the GPL crates makes the app GPL-3; the alternative is reimplementing (~300 lines of crypto).
- Native deps (libheif/ffmpeg) have to build in CI on three OSes.
- Rust is not installed on the dev PC yet.
