from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional


# Western Indonesian Time (WIB) is a fixed UTC+7 offset (no DST).
WIB = timezone(timedelta(hours=7))


def to_wib(value: Any) -> Optional[datetime]:
    """Convert a source timestamp to a naive WIB (UTC+7) datetime.

    The callback source stores timestamps in UTC (e.g. "2026-07-16T03:26:40+00:00").
    Business `created`/`updated` times should read in local WIB wall-clock time, so
    parse the value, shift it to UTC+7, and drop the tzinfo (the DB columns are
    naive DATETIME). Doing this explicitly keeps `created_at` in WIB regardless of
    the database server's own timezone. A timestamp without an offset is assumed to
    already be UTC; empty or unparseable input returns None.
    """
    if value in (None, ""):
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(WIB).replace(tzinfo=None)
