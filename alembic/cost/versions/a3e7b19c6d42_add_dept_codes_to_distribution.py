"""add dept_code and new_dept_code to distribution

The output named both departments but coded neither, so a snapshot could not be
joined back to the department master without going through a name — the one
thing that does not survive a rename.

``dept_code`` is carried from the GL line. ``new_dept_code`` is resolved from
the PC master, because the receiving department is chosen by ALLOCATION, which
is authored by name.

Revision ID: a3e7b19c6d42
Revises: f2c9d8a41e57
Create Date: 2026-07-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a3e7b19c6d42'
down_revision: Union[str, None] = 'f2c9d8a41e57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("distribution", sa.Column("dept_code", sa.String(50), nullable=True))
    op.add_column("distribution", sa.Column("new_dept_code", sa.String(50), nullable=True))
    op.create_index("ix_distribution_dept_code", "distribution", ["dept_code"])
    op.create_index("ix_distribution_new_dept_code", "distribution", ["new_dept_code"])


def downgrade() -> None:
    op.drop_index("ix_distribution_new_dept_code", table_name="distribution")
    op.drop_index("ix_distribution_dept_code", table_name="distribution")
    op.drop_column("distribution", "new_dept_code")
    op.drop_column("distribution", "dept_code")
