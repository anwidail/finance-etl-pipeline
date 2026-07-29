"""add reporting and coa_code to gl

Revision ID: 4b48483a00ad
Revises: f3811479c72a
Create Date: 2026-07-06 16:18:41.736385
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '4b48483a00ad'
down_revision: Union[str, None] = 'f3811479c72a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# NOTE: autogenerate also proposed `op.drop_table('coa')` because coa is an
# external reference table not mapped by our models. It is intentionally NOT
# managed here — this migration only adds the two reporting columns to gl.


def upgrade() -> None:
    op.add_column('gl', sa.Column('coa_code', sa.String(length=50), nullable=True))
    op.add_column('gl', sa.Column('reporting', sa.String(length=50), nullable=True))
    op.create_index(op.f('ix_gl_coa_code'), 'gl', ['coa_code'], unique=False)
    op.create_index(op.f('ix_gl_reporting'), 'gl', ['reporting'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_gl_reporting'), table_name='gl')
    op.drop_index(op.f('ix_gl_coa_code'), table_name='gl')
    op.drop_column('gl', 'reporting')
    op.drop_column('gl', 'coa_code')
