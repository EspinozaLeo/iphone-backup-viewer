"""Folder names for per-backup caches (thumbnails now, decrypted copies from iteration 2)."""

from __future__ import annotations

import hashlib
import os
import re

from ._time import as_utc
from .models import BackupInfo

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def backup_cache_key(info: BackupInfo) -> str:
    """One cache folder per backup *snapshot*, not per phone.

    Archived backups of the same phone (iTunes "Archive", folders like `<UDID>-20120304-…`) share a
    UDID, but the same file ID can hold different contents in each. Keying by UDID plus the backup's
    own date keeps them apart. Two copies of one snapshot, in different folders, share a key, which
    is correct: their contents are identical.

    With no known backup date, the key falls back to a hash of the resolved folder path.
    """
    udid = safe_name(info.udid)
    if info.last_backup is not None:
        return f"{udid}-{as_utc(info.last_backup):%Y%m%dT%H%M%SZ}"
    try:
        where = info.path.resolve()
    except OSError:
        where = info.path.absolute()
    digest = hashlib.sha1(os.path.normcase(str(where)).encode("utf-8", "surrogatepass")).hexdigest()
    return f"{udid}-{digest[:12]}"


def safe_name(name: str) -> str:
    """Keep plist- or manifest-supplied names from escaping a cache folder."""
    cleaned = _UNSAFE.sub("_", name).strip(".")
    return cleaned or "_"
