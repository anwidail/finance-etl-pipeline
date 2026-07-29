"""rename basis_pc dept_div to div (data-preserving)

Revision ID: 3f74a6941f6e
Revises: 6b6988a19276
Create Date: 2026-07-22 17:57:20.538363
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f74a6941f6e'
down_revision: Union[str, None] = '6b6988a19276'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # basis_pc.div was renamed to dept_div out-of-band, breaking --basis-from-db
    # (model + original migration both use `div`). Rename back, preserving data
    # (CHANGE COLUMN, not drop+add), only if the drifted column is present.
    conn = op.get_bind()
    cols = [r[0] for r in conn.exec_driver_sql("SHOW COLUMNS FROM basis_pc")]
    if "dept_div" in cols and "div" not in cols:
        op.alter_column("basis_pc", "dept_div", new_column_name="div",
                        existing_type=sa.String(length=200), existing_nullable=True)


def downgrade() -> None:
    conn = op.get_bind()
    cols = [r[0] for r in conn.exec_driver_sql("SHOW COLUMNS FROM basis_pc")]
    if "div" in cols and "dept_div" not in cols:
        op.alter_column("basis_pc", "div", new_column_name="dept_div",
                        existing_type=sa.String(length=200), existing_nullable=True)
