"""change all remaining period columns to date

Makes every ``period`` column in cost_distribution_db a DATE anchored at the 1st
of the month, matching ``distribution.period`` (converted earlier). Covers the
monthly basis tables (basis_allocation, basis_fte, basis_rev), the GL fact
(gl_entry), the run log (distribution_run) and the period lock (period_close).

Existing 'MMM-YYYY' labels (e.g. 'APR-2026') are converted first with
STR_TO_DATE so the type change never fails on a non-date string; unconvertible
values become NULL (only distribution_run tolerates NULL — the basis/GL/lock
tables are always seeded with a valid period).

Revision ID: a1b2c3d4e5f6
Revises: 3f74a6941f6e
Create Date: 2026-07-24 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '3f74a6941f6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column-is-nullable) — NOT NULL for the always-seeded tables, nullable
# only for distribution_run (period may be unknown when a batch spans months).
_NOT_NULL = ["basis_allocation", "basis_fte", "basis_rev", "gl_entry", "period_close"]
_NULLABLE = ["distribution_run"]


def _to_date(table: str) -> None:
    op.execute(
        f"UPDATE {table} "
        f"SET period = STR_TO_DATE(CONCAT('01-', period), '%d-%b-%Y') "
        f"WHERE period IS NOT NULL"
    )


def _to_label(table: str) -> None:
    op.execute(
        f"UPDATE {table} "
        f"SET period = UPPER(DATE_FORMAT(period, '%b-%Y')) "
        f"WHERE period IS NOT NULL"
    )


def upgrade() -> None:
    for table in _NOT_NULL:
        _to_date(table)
        op.alter_column(
            table, "period",
            existing_type=mysql.VARCHAR(length=10),
            type_=sa.Date(),
            existing_nullable=False,
        )
    for table in _NULLABLE:
        _to_date(table)
        op.alter_column(
            table, "period",
            existing_type=mysql.VARCHAR(length=10),
            type_=sa.Date(),
            existing_nullable=True,
        )


def downgrade() -> None:
    for table in _NOT_NULL:
        op.alter_column(
            table, "period",
            existing_type=sa.Date(),
            type_=mysql.VARCHAR(length=10),
            existing_nullable=False,
        )
        _to_label(table)
    for table in _NULLABLE:
        op.alter_column(
            table, "period",
            existing_type=sa.Date(),
            type_=mysql.VARCHAR(length=10),
            existing_nullable=True,
        )
        _to_label(table)
