"""change original_currency to numeric amount

Revision ID: 371ac6a26ea3
Revises: f44e7d2a2721
Create Date: 2026-07-16 15:26:35.563212
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '371ac6a26ea3'
down_revision: Union[str, None] = 'f44e7d2a2721'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# original_currency was first added as a VARCHAR currency code; it is revised to
# hold the row `amount` expressed in the original currency (a numeric value).
# The old string values (e.g. "USD") can't be cast, so drop and re-add. The new
# column is repopulated by an ETL backfill.
TABLES = [
    "manual_journal",
    "sales_invoice",
    "sales_return",
    "receivable_payment",
    "purchase_invoice",
    "purchase_return",
    "payable_payment",
    "cash_in",
    "cash_out",
    "gl",
]


def upgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "original_currency")
        op.add_column(
            table,
            sa.Column("original_currency", sa.Numeric(18, 2), nullable=True),
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "original_currency")
        op.add_column(
            table,
            sa.Column("original_currency", sa.String(length=10), nullable=True),
        )
