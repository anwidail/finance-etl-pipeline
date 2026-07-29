"""add basis column to basis_allocation

Records where each split factor comes from, so the stored basis is
self-describing in SQL instead of only in ``Config``:

- ``basis_fte``  — headcount share by dept within the method's scope
- ``basis_rev``  — the matching revenue percentage, grouped by Div
- ``Fixed``      — hand-maintained; the recompute passes these through untouched

Existing rows are backfilled from their method name. The method lists are
spelled out here rather than imported from ``Config`` because a migration must
keep describing the world as it was at this revision, even after config moves on.

Revision ID: c8d1a4b6e920
Revises: b7c9e2f14d30
Create Date: 2026-07-27 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c8d1a4b6e920'
down_revision: Union[str, None] = 'b7c9e2f14d30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FTE_METHODS = [
    "FTE - All", "FTE - Head Office", "FTE - Laboratory",
    "FTE - Surabaya", "FTE - Medan",
    "Head Office : Tower G", "Head Office : Tower B",
]
_REV_METHODS = [
    "Revenue HO", "Revenue", "Revenue - System Certification Services",
    # Pre-V.05 spelling, still present in periods seeded from an older workbook.
    "Revenue - Certification Services",
]


def _in_list(values: list) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def upgrade() -> None:
    op.add_column("basis_allocation", sa.Column("basis", sa.String(20), nullable=True))
    op.create_index("ix_basis_allocation_basis", "basis_allocation", ["basis"])

    # Backfill: FTE-driven, revenue-driven, everything else Fixed.
    op.execute(
        f"UPDATE basis_allocation SET basis = 'basis_fte' "
        f"WHERE distribution IN ({_in_list(_FTE_METHODS)})"
    )
    op.execute(
        f"UPDATE basis_allocation SET basis = 'basis_rev' "
        f"WHERE distribution IN ({_in_list(_REV_METHODS)})"
    )
    op.execute("UPDATE basis_allocation SET basis = 'Fixed' WHERE basis IS NULL")


def downgrade() -> None:
    op.drop_index("ix_basis_allocation_basis", table_name="basis_allocation")
    op.drop_column("basis_allocation", "basis")
