"""Giving a media player a file name it can recognise.

Backup data files are named by their SHA1 file ID and have no extension. On Qt 6.12 the FFmpeg backend
probes content and plays them directly (checked on Windows with H.264/AAC MP4, H.264 MOV and HEVC MOV),
so the viewer tries the backup's own file first. If a backend refuses it, the viewer calls
`playable_alias`, which copies it to `<cache>/<backup cache key>/play/<file_id><ext>`.

Always a copy, never a hard link: a link would bump the backup file's link count and ctime, and any later
write to the cache file would land in the backup itself.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def play_dir(cache_dir: Path, cache_key: str) -> Path:
    """`cache_key` is core.cache_key.backup_cache_key(info): archived backups of one phone share a UDID."""
    return Path(cache_dir) / cache_key / "play"


def playable_alias(source: Path, cache_dir: Path, cache_key: str, file_id: str, suffix: str) -> Path:
    """An extension-bearing copy of `source`. Blocking (a copy can be large): call it from a worker.
    Reuses an existing copy of the same size."""
    source = Path(source)
    suffix = suffix if suffix.startswith(".") or not suffix else "." + suffix
    target = play_dir(cache_dir, cache_key) / f"{file_id}{suffix.lower()}"
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
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return target


def release_aliases(cache_dir: Path, cache_key: str) -> None:
    """Delete one backup's play copies; they can be large."""
    shutil.rmtree(play_dir(cache_dir, cache_key), ignore_errors=True)
