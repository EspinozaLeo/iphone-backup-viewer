"""Human-readable text for dates, durations and counts. Dates from the contract are UTC; we show local time."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, QDateTime, QLocale, QTime


def _local_qdatetime(dt: datetime) -> QDateTime:
    local = dt.astimezone()  # aware UTC -> this machine's zone (a naive value is taken as local already)
    return QDateTime(QDate(local.year, local.month, local.day), QTime(local.hour, local.minute, local.second))


def short_datetime(dt: datetime | None, locale: QLocale | None = None) -> str:
    """e.g. "Mar 3, 2014, 4:12 PM"."""
    if dt is None:
        return "Unknown date"
    loc = locale or QLocale()
    q = _local_qdatetime(dt)
    time = loc.toString(q.time(), loc.timeFormat(QLocale.FormatType.ShortFormat))
    return f"{loc.toString(q.date(), 'MMM d, yyyy')}, {time}"


def long_datetime(dt: datetime | None, locale: QLocale | None = None) -> str:
    """e.g. "Monday, March 3, 2014 · 4:12 PM"."""
    if dt is None:
        return "Unknown date"
    loc = locale or QLocale()
    q = _local_qdatetime(dt)
    date = loc.toString(q.date(), QLocale.FormatType.LongFormat)
    time = loc.toString(q.time(), loc.timeFormat(QLocale.FormatType.ShortFormat))
    return f"{date} · {time}"


def month_year(dt: datetime | None, locale: QLocale | None = None) -> str:
    if dt is None:
        return ""
    loc = locale or QLocale()
    return loc.toString(_local_qdatetime(dt).date(), "MMMM yyyy")


def year(dt: datetime | None) -> int | None:
    return None if dt is None else dt.astimezone().year


def duration_text(ms: int) -> str:
    seconds = max(0, int(ms) // 1000)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n:,} {singular if n == 1 else (plural or singular + 's')}"


def error_text(exc: BaseException | None) -> str:
    """An exception as a short line for the UI; some (NotImplementedError()) have an empty str()."""
    if exc is None:
        return ""
    return str(exc) or type(exc).__name__
