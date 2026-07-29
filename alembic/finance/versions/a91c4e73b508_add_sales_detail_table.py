"""add sales_detail table (manually imported sales invoices)

Zahir has no callback for sales invoices, so that module cannot be fed the way
the other eight are. ``sales_detail`` is the stand-in document store: invoice
lines imported from a workbook, one row per service line, from which the journal
in ``gl`` is generated under module ``sales_detail``.

Unique on (``ref_no``, ``service_code``) so a re-import refreshes a document
instead of duplicating it.

``currency``/``exchange_rate`` are not in the source workbook but are carried
here because the journal depends on them: a non-IDR invoice books its receivable
to Foreign A/R (1121-12-000) instead of Domestic A/R (1121-11-000). Both default
to the IDR case.

Revision ID: a91c4e73b508
Revises: 5394eea6185b
Create Date: 2026-07-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a91c4e73b508'
down_revision: Union[str, None] = '5394eea6185b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sales_detail",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),

        sa.Column("date", sa.Date, nullable=False),
        sa.Column("ref_no", sa.String(100), nullable=False),
        sa.Column("contact", sa.String(200), nullable=True),
        sa.Column("dept", sa.String(200), nullable=True),
        sa.Column("description", sa.Text, nullable=True),

        sa.Column("service_code", sa.String(50), nullable=False),
        sa.Column("service_name", sa.String(200), nullable=True),
        sa.Column("service", sa.String(260), nullable=True),

        sa.Column("account", sa.String(200), nullable=True),
        sa.Column("account_code", sa.String(50), nullable=True),

        sa.Column("subtotal", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("tax", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(18, 2), nullable=False, server_default="0"),

        sa.Column("currency", sa.String(10), nullable=False, server_default="IDR"),
        sa.Column("exchange_rate", sa.Numeric(18, 6), nullable=False, server_default="1"),

        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("period", sa.Date, nullable=True),

        sa.Column("income_type", sa.String(100), nullable=True),
        sa.Column("classification", sa.String(100), nullable=True),
        sa.Column("location", sa.String(100), nullable=True),
        sa.Column("reporting_line", sa.String(200), nullable=True),

        sa.Column("new_dept", sa.String(200), nullable=True),
        sa.Column("dept_div", sa.String(200), nullable=True),
        sa.Column("pc", sa.String(200), nullable=True),

        sa.Column("imported_at", sa.DateTime, nullable=True),

        sa.UniqueConstraint("ref_no", "service_code", name="uq_sales_detail_ref_service"),
    )
    for column in ("date", "ref_no", "contact", "dept", "service_code", "account",
                   "account_code", "status", "created_at", "period",
                   "new_dept", "pc"):
        op.create_index(f"ix_sales_detail_{column}", "sales_detail", [column])


def downgrade() -> None:
    op.drop_table("sales_detail")
