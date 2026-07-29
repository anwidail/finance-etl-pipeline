"""maintain created_at/updated_at automatically on every basis table

The basis tables are edited both by the loaders and by hand in SQL. Only the
loaders were stamping ``created_at``/``updated_at``, so a manual INSERT left
them NULL and a manual UPDATE left ``updated_at`` stale — the audit trail was
only as good as the tool you happened to use.

This revision moves the stamping into the database:

- insert -> ``DEFAULT (UTC_TIMESTAMP())`` on both columns
- update -> a ``BEFORE UPDATE`` trigger sets ``updated_at``

**UTC, deliberately.** The loaders write ``datetime.now(timezone.utc)`` and
every existing row is UTC, but this server runs ``time_zone = SYSTEM`` (UTC+7),
so the conventional ``CURRENT_TIMESTAMP`` default would stamp local time and put
a 7-hour step in the middle of the history. ``UTC_TIMESTAMP()`` keeps one clock.
MySQL only accepts ``CURRENT_TIMESTAMP`` in an ``ON UPDATE`` clause, which is
why ``updated_at`` needs a trigger rather than a column attribute.

Rows whose stamps are NULL (hand-inserted) are backfilled before the columns are
made NOT NULL: ``created_at`` gets the migration time — the insert time is not
recoverable — and ``updated_at`` follows ``created_at``.

Revision ID: d4e6f8a02b17
Revises: c8d1a4b6e920
Create Date: 2026-07-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4e6f8a02b17'
down_revision: Union[str, None] = 'c8d1a4b6e920'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Every table carrying the _RefMixin stamps, including the GL fact.
_TABLES = [
    "basis_pc", "basis_coa", "basis_logic", "basis_rl",
    "basis_allocation", "basis_fte", "basis_rev", "gl_entry",
]


def upgrade() -> None:
    for table in _TABLES:
        # 1. Backfill so the columns can become NOT NULL.
        op.execute(f"UPDATE {table} SET created_at = UTC_TIMESTAMP() "
                   f"WHERE created_at IS NULL")
        op.execute(f"UPDATE {table} SET updated_at = created_at "
                   f"WHERE updated_at IS NULL")

        # 2. Insert-time default.
        op.execute(f"ALTER TABLE {table} "
                   f"MODIFY created_at DATETIME NOT NULL DEFAULT (UTC_TIMESTAMP())")
        op.execute(f"ALTER TABLE {table} "
                   f"MODIFY updated_at DATETIME NOT NULL DEFAULT (UTC_TIMESTAMP())")

        # 3. Update-time stamp. Unconditional: the database owns this column, so
        #    a caller passing its own value does not get to keep it.
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at")
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW SET NEW.updated_at = UTC_TIMESTAMP()"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at")
        op.execute(f"ALTER TABLE {table} MODIFY created_at DATETIME NULL DEFAULT NULL")
        op.execute(f"ALTER TABLE {table} MODIFY updated_at DATETIME NULL DEFAULT NULL")
