"""Timestamp helpers shared by discovery and the manifest readers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def as_utc(value: datetime) -> datetime:
    """Plists store UTC, and plistlib hands it back naive. Make it aware without shifting it."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_from_unix(seconds: object) -> datetime | None:
    """Unix seconds to an aware UTC datetime. None for zero, negative, or nonsense values.

    Uses epoch arithmetic rather than datetime.fromtimestamp, which raises OSError on Windows
    for values outside the C runtime's range.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    try:
        return _EPOCH + timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        return None
