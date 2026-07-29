"""add activity_log table

Revision ID: 5394eea6185b
Revises: 371ac6a26ea3
Create Date: 2026-07-17 09:31:40.862880
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5394eea6185b'
down_revision: Union[str, None] = '371ac6a26ea3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "activity_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column("created_by_email", sa.String(length=200), nullable=True),
        sa.Column("activity_type", sa.String(length=20), nullable=True),
        sa.Column("action", sa.String(length=20), nullable=True),
        sa.Column("module", sa.String(length=50), nullable=True),
        sa.Column("endpoint", sa.String(length=50), nullable=True),
        sa.Column("ref_no", sa.String(length=100), nullable=True),
        sa.Column("source_id", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("doc_date", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("callback_id", sa.String(length=100), nullable=False),
        sa.Column("callback_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("callback_id", name="uq_activity_log_callback"),
    )
    op.create_index(op.f("ix_activity_log_created_date"), "activity_log", ["created_date"], unique=False)
    op.create_index(op.f("ix_activity_log_created_by"), "activity_log", ["created_by"], unique=False)
    op.create_index(op.f("ix_activity_log_activity_type"), "activity_log", ["activity_type"], unique=False)
    op.create_index(op.f("ix_activity_log_module"), "activity_log", ["module"], unique=False)
    op.create_index(op.f("ix_activity_log_ref_no"), "activity_log", ["ref_no"], unique=False)
    op.create_index(op.f("ix_activity_log_source_id"), "activity_log", ["source_id"], unique=False)
    op.create_index(op.f("ix_activity_log_status"), "activity_log", ["status"], unique=False)
    op.create_index(op.f("ix_activity_log_doc_date"), "activity_log", ["doc_date"], unique=False)
    op.create_index(op.f("ix_activity_log_callback_at"), "activity_log", ["callback_at"], unique=False)


def downgrade() -> None:
    op.drop_table("activity_log")
