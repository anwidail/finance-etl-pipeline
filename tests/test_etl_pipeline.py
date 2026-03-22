"""Tests for ETL pipeline functions — extract, transform, load."""

from datetime import datetime

import pandas as pd
from sqlalchemy import text

from transforms import transform
from etl_pipeline import load


def _make_transactions(rows):
    """Helper: build a DataFrame that looks like extract() output."""
    defaults = {
        "transaction_id": 1,
        "customer_id": 1,
        "customer_name": "Test Customer",
        "product_id": 1,
        "product_name": "Test Product",
        "product_category": "Electronics",
        "quantity": 1,
        "unit_price": 1000000.00,
        "total_amount": 1000000.00,
        "status": "completed",
        "created_at": "2026-03-10 10:00:00",
    }
    data = []
    for i, overrides in enumerate(rows):
        row = {**defaults, "transaction_id": i + 1, **overrides}
        data.append(row)
    return pd.DataFrame(data)


class TestTransform:
    def test_filters_only_completed(self):
        df = _make_transactions([
            {"status": "completed"},
            {"status": "pending"},
            {"status": "cancelled"},
        ])
        result = transform(df)
        # 1 completed day → 3 account rows
        assert len(result) == 3

    def test_empty_when_no_completed(self):
        df = _make_transactions([
            {"status": "pending"},
            {"status": "cancelled"},
        ])
        result = transform(df)
        assert result.empty

    def test_empty_dataframe(self):
        df = pd.DataFrame(columns=[
            "transaction_id", "customer_id", "customer_name", "product_id",
            "product_name", "product_category", "quantity", "unit_price",
            "total_amount", "status", "created_at",
        ])
        result = transform(df)
        assert result.empty

    def test_produces_three_rows_per_day(self):
        df = _make_transactions([
            {"created_at": "2026-03-10 09:00:00"},
            {"created_at": "2026-03-10 14:00:00"},
            {"created_at": "2026-03-11 10:00:00"},
        ])
        result = transform(df)
        # 2 days × 3 accounts = 6 rows
        assert len(result) == 6

    def test_account_ids(self):
        df = _make_transactions([{"total_amount": 1000000.00}])
        result = transform(df)
        account_ids = sorted(result["account_id"].tolist())
        assert account_ids == [1, 2, 3]

    def test_ppn_calculation(self):
        df = _make_transactions([{"total_amount": 1000000.00}])
        result = transform(df)

        ar_row = result[result["account_id"] == 1].iloc[0]
        rev_row = result[result["account_id"] == 2].iloc[0]
        tax_row = result[result["account_id"] == 3].iloc[0]

        # tax = 1_000_000 * 0.11 = 110_000
        # net = 1_000_000 - 110_000 = 890_000
        # gross = 1_000_000 + 110_000 = 1_110_000
        assert ar_row["total_debit"] == 1_110_000.00
        assert rev_row["total_credit"] == 890_000.00
        assert tax_row["total_credit"] == 110_000.00

    def test_transaction_count(self):
        df = _make_transactions([
            {"created_at": "2026-03-10 09:00:00"},
            {"created_at": "2026-03-10 14:00:00"},
            {"created_at": "2026-03-10 16:00:00"},
        ])
        result = transform(df)
        # All 3 account rows for that day should have count = 3
        for _, row in result.iterrows():
            assert row["transaction_count"] == 3

    def test_has_etl_loaded_at(self):
        df = _make_transactions([{}])
        result = transform(df)
        assert "etl_loaded_at" in result.columns
        assert isinstance(result["etl_loaded_at"].iloc[0], datetime)

    def test_output_columns(self):
        df = _make_transactions([{}])
        result = transform(df)
        expected = {
            "summary_date", "account_id", "total_debit",
            "total_credit", "net_amount", "transaction_count", "etl_loaded_at",
        }
        assert set(result.columns) == expected


class TestLoad:
    def test_load_append(self, finance_engine):
        df = pd.DataFrame({"col_a": [1, 2, 3], "col_b": ["x", "y", "z"]})
        load(df, finance_engine, table_name="test_load")

        with finance_engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM test_load"))
            assert result.scalar() == 3

    def test_load_append_adds_rows(self, finance_engine):
        df1 = pd.DataFrame({"val": [1, 2]})
        df2 = pd.DataFrame({"val": [3, 4, 5]})
        load(df1, finance_engine, table_name="test_append")
        load(df2, finance_engine, table_name="test_append")

        with finance_engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM test_append"))
            assert result.scalar() == 5

    def test_load_replace(self, finance_engine):
        df1 = pd.DataFrame({"val": [1, 2, 3]})
        df2 = pd.DataFrame({"val": [10]})
        load(df1, finance_engine, table_name="test_replace")
        load(df2, finance_engine, table_name="test_replace", mode="replace")

        with finance_engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM test_replace"))
            assert result.scalar() == 1
