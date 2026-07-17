"""
Finance Database (finance_db) — Models

Destination database for ETL output:
- parsed callback layer
- transaction tables
- accounting layer
- ETL audit log
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Numeric, Date, DateTime, Text,
    UniqueConstraint, Index, create_engine
)
from sqlalchemy.orm import declarative_base, sessionmaker

FinanceBase = declarative_base()


class TransactionLineMixin:
    date = Column(Date, nullable=True, index=True)
    type = Column(String(50), nullable=False, index=True)
    ref_no = Column(String(100), nullable=False, index=True)

    contact = Column(String(200), nullable=True, index=True)
    description = Column(Text, nullable=True)
    note = Column(Text, nullable=True)

    department = Column(String(200), nullable=True, index=True)
    project = Column(String(200), nullable=True, index=True)

    debit = Column(Numeric(18, 2), nullable=False, default=0)
    credit = Column(Numeric(18, 2), nullable=False, default=0)
    amount = Column(Numeric(18, 2), nullable=False, default=0)

    account_code = Column(String(50), nullable=True, index=True)
    coa_name = Column(String(200), nullable=True)

    source_id = Column(String(100), nullable=False, index=True)
    source_line_id = Column(String(100), nullable=True, index=True)

    # `currency` is the line/booking currency (its code); `original_currency`
    # holds the row `amount` expressed in that original currency (amount /
    # exchange_rate — the foreign-currency value for FX documents, else = amount).
    currency = Column(String(10), nullable=True, default="IDR")
    original_currency = Column(Numeric(18, 2), nullable=True)
    exchange_rate = Column(Numeric(18, 6), nullable=True, default=1)

    status = Column(String(50), nullable=True, index=True)
    created_at = Column(DateTime, nullable=True, index=True)
    created_by = Column(String(200), nullable=True)


class SalesReferenceMixin:
    sales_ref_no = Column(String(100), nullable=True, index=True)
    sales_date = Column(Date, nullable=True, index=True)
    sales_id = Column(String(100), nullable=True, index=True)


class PurchaseReferenceMixin:
    purchase_ref_no = Column(String(100), nullable=True, index=True)
    purchase_date = Column(Date, nullable=True, index=True)
    purchase_id = Column(String(100), nullable=True, index=True)


class ManualJournal(FinanceBase, TransactionLineMixin):
    __tablename__ = "manual_journal"
    id = Column(Integer, primary_key=True, autoincrement=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_line_id", name="uq_manual_journal_source_line"),
    )


class SalesInvoice(FinanceBase, TransactionLineMixin):
    __tablename__ = "sales_invoice"
    id = Column(Integer, primary_key=True, autoincrement=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_line_id", name="uq_sales_invoice_source_line"),
    )


class SalesReturn(FinanceBase, TransactionLineMixin, SalesReferenceMixin):
    __tablename__ = "sales_return"
    id = Column(Integer, primary_key=True, autoincrement=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_line_id", name="uq_sales_return_source_line"),
    )


class ReceivablePayment(FinanceBase, TransactionLineMixin, SalesReferenceMixin):
    __tablename__ = "receivable_payment"
    id = Column(Integer, primary_key=True, autoincrement=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_line_id", "sales_ref_no", name="uq_receivable_payment_source_line"),
    )


class PurchaseInvoice(FinanceBase, TransactionLineMixin):
    __tablename__ = "purchase_invoice"
    id = Column(Integer, primary_key=True, autoincrement=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_line_id", name="uq_purchase_invoice_source_line"),
    )


class PurchaseReturn(FinanceBase, TransactionLineMixin, PurchaseReferenceMixin):
    __tablename__ = "purchase_return"
    id = Column(Integer, primary_key=True, autoincrement=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_line_id", name="uq_purchase_return_source_line"),
    )


class PayablePayment(FinanceBase, TransactionLineMixin, PurchaseReferenceMixin):
    __tablename__ = "payable_payment"
    id = Column(Integer, primary_key=True, autoincrement=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_line_id", "purchase_ref_no", name="uq_payable_payment_source_line"),
    )


class CashIn(FinanceBase, TransactionLineMixin):
    __tablename__ = "cash_in"
    id = Column(Integer, primary_key=True, autoincrement=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_line_id", name="uq_cash_in_source_line"),
    )


class CashOut(FinanceBase, TransactionLineMixin):
    __tablename__ = "cash_out"
    id = Column(Integer, primary_key=True, autoincrement=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_line_id", name="uq_cash_out_source_line"),
    )


class EtlState(FinanceBase):
    """Key-value store for ETL run state.

    Holds the incremental extraction watermark: the max source `created_at`
    already processed. Each run pulls only callbacks newer than this (minus a
    small overlap), so new/edited/cancelled transactions are never left behind
    while old, untouched history is not re-scanned every run.
    """

    __tablename__ = "etl_state"

    state_key = Column(String(100), primary_key=True)
    watermark = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class ActivityLog(FinanceBase):
    """User activity / audit trail — one row per source callback (action).

    Every finance-module callback is recorded as an activity: who performed what
    action (create / update / delete) on which document, and when. This is
    independent of the ledger load (which keeps only approved, latest,
    non-deleted rows), so drafts, edits and deletes all appear here. Keyed on
    `callback_id` for idempotent upserts across re-runs and backfills.

    `created_date` / `created_by` describe the action itself (the actor and time
    of this callback's change — `updated`, falling back to `created`), not
    necessarily the document's original creation.
    """

    __tablename__ = "activity_log"
    id = Column(Integer, primary_key=True, autoincrement=True)

    created_date = Column(DateTime, nullable=True, index=True)
    created_by = Column(String(200), nullable=True, index=True)
    created_by_email = Column(String(200), nullable=True)

    activity_type = Column(String(20), nullable=True, index=True)  # create/update/delete
    action = Column(String(20), nullable=True)  # POST/PUT/PATCH/DELETE

    module = Column(String(50), nullable=True, index=True)
    endpoint = Column(String(50), nullable=True)

    ref_no = Column(String(100), nullable=True, index=True)
    source_id = Column(String(100), nullable=True, index=True)
    status = Column(String(50), nullable=True, index=True)
    doc_date = Column(Date, nullable=True, index=True)
    note = Column(Text, nullable=True)

    callback_id = Column(String(100), nullable=False)
    callback_at = Column(DateTime, nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint("callback_id", name="uq_activity_log_callback"),
    )


class GeneralLedger(FinanceBase, TransactionLineMixin):
    """Unified general ledger — every module's line rows land here.

    Each of the nine subledger tables is written as usual, and the same
    transformed rows are also upserted here so the whole company posts to a
    single ledger. Because `source_id`/`source_line_id` are only unique within a
    module, the business key is scoped by `type` (the source endpoint, e.g.
    "sales_invoices"). `ref_key` disambiguates payment allocations where one
    payment line is split across several invoices/purchases (same source_line_id,
    different reference document).
    """

    __tablename__ = "gl"
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Module discriminator (loader key, e.g. "sales_invoice"). `type` above holds
    # the source endpoint; `module` mirrors the ETL module name for reporting.
    module = Column(String(50), nullable=False, index=True)

    # Reference document key for payment allocations; "" when not applicable.
    ref_key = Column(String(200), nullable=False, default="")

    # Reporting enrichment joined from accounting.coa. gl account codes are a
    # simplified form of the coa codes (dashes dropped, trailing zeros trimmed),
    # so the join is done on a normalized key — see load/gl_reporting.py.
    # `coa_code` is the matched original coa account_code (the reconciled
    # reference); `reporting` is the coa statement group (e.g. "Balance Sheet").
    coa_code = Column(String(50), nullable=True, index=True)
    reporting = Column(String(50), nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint(
            "type", "source_id", "source_line_id", "ref_key", name="uq_gl_type_source_line"
        ),
    )