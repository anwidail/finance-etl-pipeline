"""V.05 workbook: reporting code/account, RL master, dept_div rename

Brings cost_distribution_db up to the "Automated Cost Distribution V.05" shape:

- ``distribution`` gains ``reporting_code`` / ``reporting_account`` (the COA
  reporting line's id and its "<code> <name>" label) and renames ``div`` to
  ``dept_div``, matching the workbook's renamed output column.
- ``basis_coa`` gains the same two columns — V.05's COA sheet resolves every
  account to a reporting code, not just a reporting line name.
- ``basis_rl`` is new: the reporting-line master (V.05 sheet ``RL``), the
  hierarchy those reporting codes belong to.
- ``gl_entry`` gains ``note2``, the reviewer annotation column V.05 added to GL.

Adding columns and renaming one is non-destructive; ``downgrade`` reverses all of
it, dropping the added columns (their values are re-derivable from COA).

Revision ID: b7c9e2f14d30
Revises: a1b2c3d4e5f6
Create Date: 2026-07-27 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7c9e2f14d30'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- distribution -----------------------------------------------------
    op.add_column("distribution", sa.Column("reporting_code", sa.String(50), nullable=True))
    op.add_column("distribution", sa.Column("reporting_account", sa.String(260), nullable=True))
    op.create_index("ix_distribution_reporting_code", "distribution", ["reporting_code"])
    # MySQL needs the full column spec on a rename.
    op.alter_column("distribution", "div",
                    new_column_name="dept_div",
                    existing_type=sa.String(200), existing_nullable=True)

    # --- basis_coa --------------------------------------------------------
    op.add_column("basis_coa", sa.Column("reporting_code", sa.String(50), nullable=True))
    op.add_column("basis_coa", sa.Column("reporting_account", sa.String(260), nullable=True))
    op.create_index("ix_basis_coa_reporting_code", "basis_coa", ["reporting_code"])

    # --- basis_rl (new) ---------------------------------------------------
    op.create_table(
        "basis_rl",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("head_code", sa.String(50), nullable=True),
        sa.Column("head_description", sa.String(200), nullable=True),
        sa.Column("reporting_line", sa.String(50), nullable=True),
        sa.Column("reporting_line_name", sa.String(200), nullable=True),
        sa.Column("reporting_code", sa.String(50), nullable=True),
        sa.Column("description", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_basis_rl_head_code", "basis_rl", ["head_code"])
    op.create_index("ix_basis_rl_reporting_line", "basis_rl", ["reporting_line"])
    op.create_index("ix_basis_rl_reporting_code", "basis_rl", ["reporting_code"])

    # --- gl_entry ---------------------------------------------------------
    op.add_column("gl_entry", sa.Column("note2", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("gl_entry", "note2")

    op.drop_index("ix_basis_rl_reporting_code", table_name="basis_rl")
    op.drop_index("ix_basis_rl_reporting_line", table_name="basis_rl")
    op.drop_index("ix_basis_rl_head_code", table_name="basis_rl")
    op.drop_table("basis_rl")

    op.drop_index("ix_basis_coa_reporting_code", table_name="basis_coa")
    op.drop_column("basis_coa", "reporting_account")
    op.drop_column("basis_coa", "reporting_code")

    op.alter_column("distribution", "dept_div",
                    new_column_name="div",
                    existing_type=sa.String(200), existing_nullable=True)
    op.drop_index("ix_distribution_reporting_code", table_name="distribution")
    op.drop_column("distribution", "reporting_account")
    op.drop_column("distribution", "reporting_code")
