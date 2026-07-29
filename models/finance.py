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
    realization = Column(Numeric(18, 2), nullable=False, default=0)

    account_code = Column(String(50), nullable=True, index=True)
    coa_name = Column(String(200), nullable=True)

    source_id = Column(String(100), nullable=False, index=True)
    source_line_id = Column(String(100), nullable=True, index=True)

    currency = Column(String(10), nullable=True, default="IDR")
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