"""Initial finance database schema

Revision ID: 0001
Revises: None
Create Date: 2025-03-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- Chart of accounts --
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(20), unique=True, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "type",
            sa.Enum("asset", "liability", "equity", "revenue", "expense", name="account_type"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )

    # -- Journal entries (header) --
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("entry_date", sa.Date, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("reference_number", sa.String(100)),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )

    # -- Journal lines (debit/credit per account) --
    op.create_table(
        "journal_lines",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("journal_entry_id", sa.Integer, sa.ForeignKey("journal_entries.id"), nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("debit", sa.Numeric(15, 2), server_default="0"),
        sa.Column("credit", sa.Numeric(15, 2), server_default="0"),
        sa.Column("description", sa.String(500)),
    )

    # -- Daily aggregated summary (ETL target table) --
    op.create_table(
        "finance_summary",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("summary_date", sa.Date, nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("total_debit", sa.Numeric(15, 2), server_default="0"),
        sa.Column("total_credit", sa.Numeric(15, 2), server_default="0"),
        sa.Column("net_amount", sa.Numeric(15, 2), server_default="0"),
        sa.Column("transaction_count", sa.Integer, server_default="0"),
        sa.Column("etl_loaded_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("finance_summary")
    op.drop_table("journal_lines")
    op.drop_table("journal_entries")
    op.drop_table("accounts")
