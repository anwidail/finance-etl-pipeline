"""Period close / lock for the cost distribution database.

A closed period is frozen: its GL, basis and distribution snapshot cannot be
overwritten (seeding, recompute-persist, snapshot loads are refused) until it is
reopened. Reads and reports on a closed period are unaffected.
"""
from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from cost_distribution.periods import now_jakarta, period_to_date
from models.cost_distribution import PeriodClose


class PeriodClosedError(RuntimeError):
    """Raised when a write is attempted against a closed period."""


def is_period_closed(engine, period: str) -> bool:
    if not period:
        return False
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        row = session.get(PeriodClose, period_to_date(period))
        return bool(row and row.status == "closed")


def assert_period_open(engine, period: str, action: str) -> None:
    """Raise PeriodClosedError if ``period`` is closed."""
    if is_period_closed(engine, period):
        raise PeriodClosedError(
            f"Period {period} is closed; refusing to {action}. "
            f"Reopen it first (--reopen-period --period {period})."
        )


def assert_periods_open(session, period_dates, action: str) -> None:
    """Raise if any month-anchored DATE in ``period_dates`` is closed.

    The date-based counterpart to :func:`assert_period_open`. Callers that write
    rows must gate on the periods they are *actually about to touch*, not on a
    ``--period`` argument the caller may have omitted — otherwise a run with no
    period silently overwrites a closed month's snapshot.
    """
    dates = [d for d in dict.fromkeys(period_dates) if d is not None]
    if not dates:
        return
    closed = (session.query(PeriodClose)
              .filter(PeriodClose.period.in_(dates), PeriodClose.status == "closed")
              .all())
    if closed:
        names = ", ".join(sorted(str(r.period) for r in closed))
        raise PeriodClosedError(
            f"Period(s) {names} are closed; refusing to {action}. "
            f"Reopen them first (--reopen-period --period MMM-YYYY)."
        )


def close_period(engine, period: str, note: str = None) -> None:
    from load.cost_distribution_db import create_all
    create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = now_jakarta()
    period_date = period_to_date(period)  # period_close.period is a DATE
    with Session() as session:
        row = session.get(PeriodClose, period_date)
        if row:
            row.status = "closed"
            row.closed_at = now
            row.note = note
        else:
            session.add(PeriodClose(period=period_date, status="closed", closed_at=now, note=note))
        session.commit()


def reopen_period(engine, period: str) -> bool:
    """Remove the lock. Returns True if a lock existed."""
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        row = session.get(PeriodClose, period_to_date(period))
        if not row:
            return False
        session.delete(row)
        session.commit()
        return True
