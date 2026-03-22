"""
Finance Database (finance_db) — Models

This is the destination database where the ETL pipeline writes
processed and aggregated financial data.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Numeric, Date, DateTime, ForeignKey, Enum, Text
)
from sqlalchemy.orm import declarative_base, relationship

FinanceBase = declarative_base()


class Account(FinanceBase):
    """Chart of accounts — every financial entry points to one of these."""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(20), unique=True, nullable=False)  # e.g. "4100"
    name = Column(String(200), nullable=False)               # e.g. "Sales Revenue"
    type = Column(
        Enum("asset", "liability", "equity", "revenue", "expense", name="account_type"),
        nullable=False,
    )
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    journal_lines = relationship("JournalLine", back_populates="account")
    summaries = relationship("FinanceSummary", back_populates="account")


class JournalEntry(FinanceBase):
    """
    A single accounting event (e.g. a sale, a refund).
    Each entry has two or more lines that must balance (total debit == total credit).
    """
    __tablename__ = "journal_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_date = Column(Date, nullable=False)
    description = Column(Text)
    reference_number = Column(String(100))  # links back to source transaction id
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    lines = relationship("JournalLine", back_populates="journal_entry")


class JournalLine(FinanceBase):
    """Individual debit/credit line within a journal entry."""
    __tablename__ = "journal_lines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    journal_entry_id = Column(Integer, ForeignKey("journal_entries.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    debit = Column(Numeric(15, 2), default=0)
    credit = Column(Numeric(15, 2), default=0)
    description = Column(String(500))

    journal_entry = relationship("JournalEntry", back_populates="lines")
    account = relationship("Account", back_populates="journal_lines")


class FinanceSummary(FinanceBase):
    """
    Daily aggregated summary per account.
    This is the main table the ETL pipeline writes to.
    """
    __tablename__ = "finance_summary"

    id = Column(Integer, primary_key=True, autoincrement=True)
    summary_date = Column(Date, nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    total_debit = Column(Numeric(15, 2), default=0)
    total_credit = Column(Numeric(15, 2), default=0)
    net_amount = Column(Numeric(15, 2), default=0)
    transaction_count = Column(Integer, default=0)
    etl_loaded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    account = relationship("Account", back_populates="summaries")
