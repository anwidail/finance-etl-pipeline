"""Period close / lock for the cost distribution database.

A closed period is frozen: its GL, basis and distribution snapshot cannot be
overwritten (seeding, recompute-persist, snapshot loads are refused) until it is
reopened. Reads and reports on a closed period are unaffected.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from models.cost_distribution import PeriodClose


class PeriodClosedError(RuntimeError):
    """Raised when a write is attempted against a closed period."""


def is_period_closed(engine, period: str) -> bool:
    if not period:
        return False
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        row = session.get(PeriodClose, period)
        return bool(row and row.status == "closed")


def assert_period_open(engine, period: str, action: str) -> None:
    """Raise PeriodClosedError if ``period`` is closed."""
    if is_period_closed(engine, period):
        raise PeriodClosedError(
            f"Period {period} is closed; refusing to {action}. "
            f"Reopen it first (--reopen-period --period {period})."
        )


def close_period(engine, period: str, note: str = None) -> None:
    from load.cost_distribution_db import create_all
    create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc)
    with Session() as session:
        row = session.get(PeriodClose, period)
        if row:
            row.status = "closed"
            row.closed_at = now
            row.note = note
        else:
            session.add(PeriodClose(period=period, status="closed", closed_at=now, note=note))
        session.commit()


def reopen_period(engine, period: str) -> bool:
    """Remove the lock. Returns True if a lock existed."""
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        row = session.get(PeriodClose, period)
        if not row:
            return False
        session.delete(row)
        session.commit()
        return True
