"""add dept_code to gl_entry

The distribution resolves a line's PC bucket from its department. Keying that on
the name breaks whenever a department is renamed — "Automotive (…Technical Ops)"
and "CERTIFICATION SERVICES" no longer appear in the PC master at all, so those
lines silently fell through to a direct charge. Carrying the code lets the
lookup key on what does not move.

Nullable: the sales_detail feed is not callback-fed and has no code, so the
pipeline falls back to the name for those rows.

Revision ID: f2c9d8a41e57
Revises: e5f7a91c3d02
Create Date: 2026-07-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f2c9d8a41e57'
down_revision: Union[str, None] = 'e5f7a91c3d02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("gl_entry", sa.Column("dept_code", sa.String(50), nullable=True))
    op.create_index("ix_gl_entry_dept_code", "gl_entry", ["dept_code"])


def downgrade() -> None:
    op.drop_index("ix_gl_entry_dept_code", table_name="gl_entry")
    op.drop_column("gl_entry", "dept_code")
