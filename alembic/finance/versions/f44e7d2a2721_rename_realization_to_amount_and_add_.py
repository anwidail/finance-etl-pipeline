"""rename realization to amount and add original_currency

Revision ID: f44e7d2a2721
Revises: 0862dc36029c
Create Date: 2026-07-16 14:17:52.670572
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f44e7d2a2721'
down_revision: Union[str, None] = '0862dc36029c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Every line table shares the same mixin columns, so the change applies to all
# subledgers plus the unified gl.
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
        # Rename in place (CHANGE COLUMN) so existing values are preserved.
        op.alter_column(
            table,
            "realization",
            new_column_name="amount",
            existing_type=sa.Numeric(18, 2),
            existing_nullable=False,
        )
        op.add_column(
            table,
            sa.Column("original_currency", sa.String(length=10), nullable=True),
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "original_currency")
        op.alter_column(
            table,
            "amount",
            new_column_name="realization",
            existing_type=sa.Numeric(18, 2),
            existing_nullable=False,
        )
