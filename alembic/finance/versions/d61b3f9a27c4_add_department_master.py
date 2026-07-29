"""add the department master table

The authority for what a ``dept_code`` means. Until now the department existed
only as a name repeated on every transaction row, and the name is not stable:
"System Certification Services" was renamed to "Integrated Management System"
and "Automotive (…Technical Ops)" to "Automotive", leaving 4,686 ledger rows
whose stored name no longer pairs with the code that produced it.

Keyed on ``dept_code`` because that is what does not move. ``dept_name`` is
unique as well — it is a label, but a label that must not be ambiguous.

Revision ID: d61b3f9a27c4
Revises: c5a8e14b7f30
Create Date: 2026-07-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd61b3f9a27c4'
down_revision: Union[str, None] = 'c5a8e14b7f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "department",
        sa.Column("dept_code", sa.String(50), primary_key=True),
        sa.Column("dept_name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("dept_name", name="uq_department_name"),
    )


def downgrade() -> None:
    op.drop_table("department")
