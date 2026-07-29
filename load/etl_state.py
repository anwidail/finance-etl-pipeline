"""Read/write ETL run state (the incremental extraction watermark)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from models.finance import EtlState

# State key for the source callback extraction watermark.
WATERMARK_KEY = "callback_watermark"


def _utcnow_naive() -> datetime:
    """Naive UTC now, to match the source's naive DATETIME columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_watermark(engine, key: str = WATERMARK_KEY) -> datetime | None:
    """Return the stored watermark, or None if never set (first run)."""
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        row = session.get(EtlState, key)
        return row.watermark if row else None


def set_watermark(engine, value: datetime, key: str = WATERMARK_KEY) -> None:
    """Upsert the watermark to `value` (max created_at processed)."""
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        row = session.get(EtlState, key)
        now = _utcnow_naive()
        if row is None:
            session.add(EtlState(state_key=key, watermark=value, updated_at=now))
        else:
            row.watermark = value
            row.updated_at = now
        session.commit()
