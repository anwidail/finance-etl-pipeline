"""sales_detail: dept_code, and imported_at stamped by the database in WIB

Two gaps closed on the manually maintained sales module.

``dept_code`` joins the department name, as everywhere else in accounting — the
name is a label, the code is the key. It also flows into the generated journal,
so the ``gl`` rows this module posts stop being the only ones without a code.

``imported_at`` is now filled by the database at insert, in **Jakarta local time
(WIB)**, matching every other stamp in this pipeline. Rows typed straight into
SQL get stamped exactly like rows the loader writes — before this, a manual
insert simply left it NULL, which is what happened to all 2,103 existing rows.

``UTC_TIMESTAMP() + INTERVAL 7 HOUR`` rather than ``CURRENT_TIMESTAMP`` so the
value is WIB regardless of the database host's own timezone.

Revision ID: e84c2b6d5019
Revises: d61b3f9a27c4
Create Date: 2026-07-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e84c2b6d5019'
down_revision: Union[str, None] = 'd61b3f9a27c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WIB = "(UTC_TIMESTAMP() + INTERVAL 7 HOUR)"


def upgrade() -> None:
    # dept_code may already exist (added by hand); add it only if it does not.
    conn = op.get_bind()
    exists = conn.execute(sa.text("""
        SELECT COUNT(*) FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_detail'
           AND COLUMN_NAME = 'dept_code'
    """)).scalar()
    if not exists:
        op.add_column("sales_detail", sa.Column("dept_code", sa.String(100), nullable=True))
        op.create_index("ix_sales_detail_dept_code", "sales_detail", ["dept_code"])

    # Existing rows were inserted before the column had a default; the true
    # import time is not recoverable, so they take the migration's own clock.
    op.execute(f"UPDATE sales_detail SET imported_at = {_WIB} WHERE imported_at IS NULL")
    op.execute(f"ALTER TABLE sales_detail "
               f"MODIFY imported_at DATETIME NOT NULL DEFAULT {_WIB}")


def downgrade() -> None:
    op.execute("ALTER TABLE sales_detail MODIFY imported_at DATETIME NULL DEFAULT NULL")
