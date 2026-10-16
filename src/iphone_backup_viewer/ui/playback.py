"""Giving a media player a file name it can recognise.

Backup data files are named by their SHA1 file ID and have no extension. On Qt 6.12 the FFmpeg backend
probes content and plays them directly (checked on Windows with H.264/AAC MP4, H.264 MOV and HEVC MOV),
so the viewer tries the backup's own file first. If a backend refuses it, the viewer calls
`playable_alias`, which makes `<cache>/<udid>/play/<file_id><ext>`: a hard link when the backup and the
cache share a volume, otherwise a copy. The backup folder itself is never written to.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def play_dir(cache_dir: Path, udid: str) -> Path:
    return Path(cache_dir) / udid / "play"


def playable_alias(source: Path, cache_dir: Path, udid: str, file_id: str, suffix: str) -> Path:
    """An extension-bearing path with the same bytes as `source`. Blocking (a copy can be large):
    call it from a worker. Reuses an existing alias of the same size."""
    source = Path(source)
    suffix = suffix if suffix.startswith(".") or not suffix else "." + suffix
    target = play_dir(cache_dir, udid) / f"{file_id}{suffix.lower()}"
    size = source.stat().st_size
    try:
        if target.stat().st_size == size:
            return target
    except FileNotFoundError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{file_id}.", suffix=".part", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.unlink()
        try:
            os.link(source, tmp)  # same volume: instant, no extra space
        except OSError:
            shutil.copyfile(source, tmp)  # different volume, or links unsupported (FAT/exFAT, network)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return target


def release_aliases(cache_dir: Path, udid: str) -> None:
    """Delete one backup's play aliases (hard links cost nothing, copies can be large)."""
    shutil.rmtree(play_dir(cache_dir, udid), ignore_errors=True)
