"""Time helpers — the period label (MMM-YYYY, e.g. APR-2026) and the wall clock.

Kept in its own module so both the pipeline and the DB loaders can derive/parse
periods, and stamp rows, without importing each other.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd

# Western Indonesian Time (Jakarta), UTC+7 with no daylight saving — so a fixed
# offset is exact, and does not depend on the host or MySQL server's own
# timezone configuration.
JAKARTA = timezone(timedelta(hours=7), name="WIB")


def now_jakarta() -> datetime:
    """Current Jakarta wall-clock time, naive — the clock every stamp uses.

    Returned without tzinfo because the ``created_at``/``updated_at`` columns are
    plain ``DATETIME``: the value stored *is* local Jakarta time. Keeping one
    clock across the loaders and the database matters more than the choice
    itself — mixed clocks make the audit trail unreadable.
    """
    return datetime.now(JAKARTA).replace(tzinfo=None)

# Fixed English month abbreviations (locale-independent).
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Indonesian month names — the workbook writes its audit labels in Indonesian
# (e.g. the overhead sweep's "FTE : PC Juni"). Hard-coded rather than taken from
# the C locale so the label is identical on every machine.
MONTHS_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni",
             "Juli", "Agustus", "September", "Oktober", "November", "Desember"]


def previous_period(period: str) -> str:
    """'JUL-2026' -> 'JUN-2026' (the month before, same MMM-YYYY form)."""
    s = normalize_period(str(period))
    idx, year = MONTHS.index(s[:3]), int(s[4:])
    return f"{MONTHS[idx - 1]}-{year if idx else year - 1}"


def month_name_id(period: str) -> str:
    """'JUN-2026' -> 'Juni' — the Indonesian month name used in audit labels."""
    return MONTHS_ID[MONTHS.index(normalize_period(str(period))[:3])]


def date_to_period(value) -> str | None:
    """One date -> 'MMM-YYYY' (e.g. 2026-04-08 -> 'APR-2026'). None if unparseable."""
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return f"{MONTHS[ts.month - 1]}-{ts.year}"


def date_to_period_series(dates: pd.Series) -> pd.Series:
    """Vectorised date -> 'MMM-YYYY' period label."""
    return pd.to_datetime(dates, errors="coerce").apply(date_to_period)


def period_to_date(period) -> "date | None":
    """'APR-2026' -> date(2026, 4, 1) — a period anchored at the 1st of the month.

    Accepts any form normalize_period accepts; None if empty/unparseable.
    """
    if not period:
        return None
    try:
        s = normalize_period(str(period))
    except ValueError:
        return None
    month = MONTHS.index(s[:3]) + 1
    year = int(s[4:])
    return date(year, month, 1)


def normalize_period(text: str) -> str:
    """Canonicalise a period string to 'MMM-YYYY'.

    Accepts 'APR-2026' (any case) or 'YYYY-MM' (e.g. '2026-04') for convenience.
    """
    s = (text or "").strip().upper()
    if re.fullmatch(r"[A-Z]{3}-\d{4}", s) and s[:3] in MONTHS:
        return s
    m = re.fullmatch(r"(\d{4})-(\d{2})", s)
    if m:
        year, mon = int(m.group(1)), int(m.group(2))
        if 1 <= mon <= 12:
            return f"{MONTHS[mon - 1]}-{year}"
    raise ValueError(f"Invalid period {text!r}; expected MMM-YYYY like APR-2026")
