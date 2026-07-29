"""move every machine timestamp to Jakarta time (WIB, UTC+7)

The stamps were UTC. They are now Jakarta local time, on the explicit request to
read them in WIB.

Switching the default alone would leave a 7-hour step in the middle of the
history, so **existing values are converted too** (``+ INTERVAL 7 HOUR``). Every
column touched here was written by the loaders in UTC, which makes the shift
exact and one-way.

Deliberately NOT converted: ``period_close.closed_at`` holds hand-entered
logical dates (``2026-02-01 00:00:00``), not a machine clock — shifting it would
turn a clean month boundary into ``07:00:00`` and mean nothing.

``UTC_TIMESTAMP() + INTERVAL 7 HOUR`` is used rather than ``CURRENT_TIMESTAMP``
so the value is WIB no matter what the MySQL host's own timezone is set to. WIB
has no daylight saving, so the fixed offset is exact. MySQL only accepts
``CURRENT_TIMESTAMP`` in an ``ON UPDATE`` clause, so ``updated_at`` keeps its
trigger.

Revision ID: e5f7a91c3d02
Revises: d4e6f8a02b17
Create Date: 2026-07-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e5f7a91c3d02'
down_revision: Union[str, None] = 'd4e6f8a02b17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_STAMPED = [
    "basis_pc", "basis_coa", "basis_logic", "basis_rl",
    "basis_allocation", "basis_fte", "basis_rev", "gl_entry",
]
# Other machine-written clocks, so the whole database reads on one timezone.
_OTHER = [("distribution", "loaded_at"), ("distribution_run", "run_at")]

_WIB = "(UTC_TIMESTAMP() + INTERVAL 7 HOUR)"


def _shift(hours: int) -> None:
    sign = "+" if hours > 0 else "-"
    n = abs(hours)
    for table in _STAMPED:
        # Drop the trigger first: it fires on this UPDATE and would stamp
        # updated_at with "now" instead of shifting the value it already holds.
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at")
        op.execute(f"UPDATE {table} SET "
                   f"created_at = created_at {sign} INTERVAL {n} HOUR, "
                   f"updated_at = updated_at {sign} INTERVAL {n} HOUR")
    for table, column in _OTHER:
        op.execute(f"UPDATE {table} SET {column} = {column} {sign} INTERVAL {n} HOUR "
                   f"WHERE {column} IS NOT NULL")


def upgrade() -> None:
    _shift(+7)
    for table in _STAMPED:
        op.execute(f"ALTER TABLE {table} "
                   f"MODIFY created_at DATETIME NOT NULL DEFAULT {_WIB}")
        op.execute(f"ALTER TABLE {table} "
                   f"MODIFY updated_at DATETIME NOT NULL DEFAULT {_WIB}")
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW SET NEW.updated_at = UTC_TIMESTAMP() + INTERVAL 7 HOUR"
        )


def downgrade() -> None:
    _shift(-7)
    for table in _STAMPED:
        op.execute(f"ALTER TABLE {table} "
                   f"MODIFY created_at DATETIME NOT NULL DEFAULT (UTC_TIMESTAMP())")
        op.execute(f"ALTER TABLE {table} "
                   f"MODIFY updated_at DATETIME NOT NULL DEFAULT (UTC_TIMESTAMP())")
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW SET NEW.updated_at = UTC_TIMESTAMP()"
        )
