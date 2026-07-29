"""add dept_code from the Zahir callback to gl and every transaction module

The callback has always carried ``department`` as ``{id, code, name}`` but only
the name was stored. The name is not a key: "System Certification Services" has
been issued under both ``D01`` and ``D010``, so once a department is renamed the
code cannot be recovered from the name. Storing the code alongside the name
keeps the link stable across renames.

Applies to the nine callback-fed module tables and to ``gl``. The column is
nullable: rows loaded before this revision have no code until they are
backfilled from the callbacks — see ``load.dept_code_backfill``.

Revision ID: c5a8e14b7f30
Revises: b73f2d95c1ae
Create Date: 2026-07-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c5a8e14b7f30'
down_revision: Union[str, None] = 'b73f2d95c1ae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Every table built on TransactionLineMixin.
_TABLES = [
    "manual_journal", "sales_invoice", "sales_return", "receivable_payment",
    "purchase_invoice", "purchase_return", "payable_payment",
    "cash_in", "cash_out", "gl",
]


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("dept_code", sa.String(50), nullable=True))
        op.create_index(f"ix_{table}_dept_code", table, ["dept_code"])


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"ix_{table}_dept_code", table_name=table)
        op.drop_column(table, "dept_code")
