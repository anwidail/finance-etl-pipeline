"""Tests for SQLAlchemy models — verifies schema and relationships."""

from datetime import date, datetime
from decimal import Decimal

from models.source import Customer, Product, Transaction
from models.finance import Account, JournalEntry, JournalLine, FinanceSummary


# ── Source DB Models ──────────────────────────────────────────


class TestCustomer:
    def test_create_customer(self, source_session):
        customer = Customer(name="John Doe", email="john@example.com", phone="08123456")
        source_session.add(customer)
        source_session.commit()

        result = source_session.query(Customer).first()
        assert result.name == "John Doe"
        assert result.email == "john@example.com"
        assert result.created_at is not None

    def test_customer_email_unique(self, source_session):
        c1 = Customer(name="A", email="same@example.com")
        c2 = Customer(name="B", email="same@example.com")
        source_session.add_all([c1, c2])

        from sqlalchemy.exc import IntegrityError
        import pytest
        with pytest.raises(IntegrityError):
            source_session.commit()


class TestProduct:
    def test_create_product(self, source_session):
        product = Product(sku="SKU-001", name="Widget", category="Electronics", price=99.99)
        source_session.add(product)
        source_session.commit()

        result = source_session.query(Product).first()
        assert result.sku == "SKU-001"
        assert float(result.price) == 99.99


class TestTransaction:
    def test_create_transaction_with_relationships(self, source_session):
        customer = Customer(name="Jane", email="jane@example.com")
        product = Product(sku="SKU-002", name="Gadget", price=50.00)
        source_session.add_all([customer, product])
        source_session.flush()

        txn = Transaction(
            customer_id=customer.id,
            product_id=product.id,
            quantity=3,
            unit_price=50.00,
            total_amount=150.00,
            status="completed",
        )
        source_session.add(txn)
        source_session.commit()

        result = source_session.query(Transaction).first()
        assert result.quantity == 3
        assert float(result.total_amount) == 150.00
        assert result.customer.name == "Jane"
        assert result.product.sku == "SKU-002"


# ── Finance DB Models ────────────────────────────────────────


class TestAccount:
    def test_create_account(self, finance_session):
        account = Account(code="4100", name="Sales Revenue", type="revenue")
        finance_session.add(account)
        finance_session.commit()

        result = finance_session.query(Account).first()
        assert result.code == "4100"
        assert result.type == "revenue"

    def test_account_code_unique(self, finance_session):
        a1 = Account(code="1000", name="Cash", type="asset")
        a2 = Account(code="1000", name="Also Cash", type="asset")
        finance_session.add_all([a1, a2])

        from sqlalchemy.exc import IntegrityError
        import pytest
        with pytest.raises(IntegrityError):
            finance_session.commit()


class TestJournalEntry:
    def test_create_entry_with_lines(self, finance_session):
        cash = Account(code="1000", name="Cash", type="asset")
        revenue = Account(code="4100", name="Sales Revenue", type="revenue")
        finance_session.add_all([cash, revenue])
        finance_session.flush()

        entry = JournalEntry(
            entry_date=date(2025, 3, 15),
            description="Sale of goods",
            reference_number="TXN-001",
        )
        finance_session.add(entry)
        finance_session.flush()

        # Debit cash, credit revenue — must balance
        line_debit = JournalLine(
            journal_entry_id=entry.id, account_id=cash.id, debit=100.00, credit=0,
        )
        line_credit = JournalLine(
            journal_entry_id=entry.id, account_id=revenue.id, debit=0, credit=100.00,
        )
        finance_session.add_all([line_debit, line_credit])
        finance_session.commit()

        result = finance_session.query(JournalEntry).first()
        assert len(result.lines) == 2
        total_debit = sum(float(l.debit) for l in result.lines)
        total_credit = sum(float(l.credit) for l in result.lines)
        assert total_debit == total_credit  # balanced entry


class TestFinanceSummary:
    def test_create_summary(self, finance_session):
        account = Account(code="4100", name="Sales Revenue", type="revenue")
        finance_session.add(account)
        finance_session.flush()

        summary = FinanceSummary(
            summary_date=date(2025, 3, 15),
            account_id=account.id,
            total_debit=0,
            total_credit=5000.00,
            net_amount=-5000.00,
            transaction_count=10,
        )
        finance_session.add(summary)
        finance_session.commit()

        result = finance_session.query(FinanceSummary).first()
        assert result.transaction_count == 10
        assert float(result.total_credit) == 5000.00
        assert result.account.code == "4100"
