"""change distribution period to date

Revision ID: 6b6988a19276
Revises: 4cd9c762e50d
Create Date: 2026-07-22 17:50:48.204547
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = '6b6988a19276'
down_revision: Union[str, None] = '4cd9c762e50d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # distribution.period was MMM-YYYY (e.g. 'APR-2026'); make it a DATE anchored
    # at the 1st of the month. Convert existing labels first so the type change
    # doesn't fail on non-date strings (unconvertible -> NULL; re-tagged on the
    # next run since distribution is a regenerable snapshot).
    op.execute(
        "UPDATE distribution "
        "SET period = STR_TO_DATE(CONCAT('01-', period), '%d-%b-%Y') "
        "WHERE period IS NOT NULL"
    )
    op.alter_column(
        "distribution", "period",
        existing_type=mysql.VARCHAR(length=10),
        type_=sa.Date(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "distribution", "period",
        existing_type=sa.Date(),
        type_=mysql.VARCHAR(length=10),
        existing_nullable=True,
    )
    # Best-effort: render dates back to MMM-YYYY (uppercased).
    op.execute(
        "UPDATE distribution "
        "SET period = UPPER(DATE_FORMAT(period, '%b-%Y')) "
        "WHERE period IS NOT NULL"
    )
