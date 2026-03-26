"""Tests for ETL sales modules wrappers and config routing."""

from __future__ import annotations

import json

import pytest

from load.module_loader_config import MODULE_LOADER_CONFIG
from load.sales_module_loader import process_records_by_module
from transforms.receivable_payment import transform_receivable_payment_source_record
from transforms.sales_invoice import transform_sales_invoice_source_record
from transforms.sales_return import transform_sales_return_source_record


def _make_sales_invoice_source_record(overrides=None):
    data = {
        "end_point": "sales_invoices",
        "data": {
            "id": "INV-1",
            "number": "SI-001",
            "date": "2026-03-10",
            "customer": {"name": "PT Customer"},
            "currency": {"code": "IDR"},
            "exchange_rate": 1,
            "status": "posted",
            "payments": [
                {
                    "id": "pay-1",
                    "amount": 111000,
                    "account": {"id": "ar-1", "code": "110101", "name": "Accounts Receivable"},
                }
            ],
            "line_items": [
                {
                    "id": "line-1",
                    "description": "Item A",
                    "subtotal_after_discount": 100000,
                    "account": {"code": "410100", "name": "Sales Revenue"},
                    "taxes": [
                        {"id": "tax-1", "code": "PPN 11%", "name": "PPN 11%", "amount": 11000}
                    ],
                }
            ],
        },
    }

    payload = data if overrides is None else {**data, **overrides}
    return {
        "callback_id": 1001,
        "body": json.dumps(payload),
        "created_at": "2026-03-10 10:00:00",
    }


def _make_receivable_payment_source_record(overrides=None):
    data = {
        "end_point": "receivable_payments",
        "data": {
            "id": "RP-1",
            "number": "RP-001",
            "date": "2026-03-11",
            "customer": {"name": "PT Customer"},
            "currency": {"code": "IDR"},
            "exchange_rate": 1,
            "status": "posted",
            "cash": {
                "amount": 100000,
                "account": {"id": "cash-1", "code": "110201", "name": "Bank"},
            },
            "line_items": [
                {
                    "id": "line-1",
                    "invoice": {"id": "INV-1", "number": "SI-001", "date": "2026-03-10"},
                    "payable": {
                        "account": {"code": "110101", "name": "Accounts Receivable"},
                        "amount": 100000,
                    },
                    "payment": {"amount": 100000},
                    "discount": {"amount": 0},
                }
            ],
        },
    }

    payload = data if overrides is None else {**data, **overrides}
    return {
        "callback_id": 1002,
        "body": json.dumps(payload),
        "created_at": "2026-03-11 11:00:00",
    }


def _make_sales_return_source_record(overrides=None):
    data = {
        "end_point": "sales_returns",
        "data": {
            "id": "SR-1",
            "number": "SR-001",
            "date": "2026-03-12",
            "customer": {"name": "PT Customer"},
            "currency": {"code": "IDR"},
            "exchange_rate": 1,
            "status": "posted",
            "parent_memo": {"id": "INV-1", "number": "SI-001", "date": "2026-03-10"},
            "line_items": [
                {
                    "id": "line-1",
                    "description": "Returned item",
                    "subtotal_after_discount": 100000,
                    "account": {"code": "410100", "name": "Sales Revenue"},
                    "taxes": [
                        {"id": "tax-1", "code": "PPN 11%", "name": "PPN 11%", "amount": 11000}
                    ],
                }
            ],
            "payments": [
                {
                    "id": "pay-1",
                    "amount": 111000,
                    "account": {"code": "110101", "name": "Accounts Receivable"},
                }
            ],
        },
    }

    payload = data if overrides is None else {**data, **overrides}
    return {
        "callback_id": 1003,
        "body": json.dumps(payload),
        "created_at": "2026-03-12 12:00:00",
    }


class TestTransformSourceRecord:
    def test_sales_invoice_source_record(self):
        source_record = _make_sales_invoice_source_record()
        rows = transform_sales_invoice_source_record(source_record)

        assert rows
        assert all(row["type"] == "sales_invoices" for row in rows)
        assert all(row["raw_callback_id"] == "1001" for row in rows)
        assert all(row["raw_created_at"] == "2026-03-10 10:00:00" for row in rows)

    def test_receivable_payment_source_record(self):
        source_record = _make_receivable_payment_source_record()
        rows = transform_receivable_payment_source_record(source_record)

        assert rows
        assert all(row["type"] == "receivable_payments" for row in rows)
        assert any(row.get("sales_ref_no") == "SI-001" for row in rows)
        assert all(row["raw_callback_id"] == "1002" for row in rows)

    def test_sales_return_source_record(self):
        source_record = _make_sales_return_source_record()
        rows = transform_sales_return_source_record(source_record)

        assert rows
        assert all(row["type"] == "sales_returns" for row in rows)
        assert any(row.get("sales_ref_no") == "SI-001" for row in rows)
        assert all(row["raw_callback_id"] == "1003" for row in rows)

    def test_invalid_endpoint_raises_value_error(self):
        source_record = _make_sales_invoice_source_record(
            overrides={"end_point": "unexpected_endpoint"}
        )

        with pytest.raises(ValueError, match="unexpected end_point"):
            transform_sales_invoice_source_record(source_record)


class TestProcessRecordsByModule:
    def test_routes_module_config_to_process_source_records(self, monkeypatch):
        captured = {}

        def fake_process_source_records(**kwargs):
            captured.update(kwargs)
            return {"processed_rows": 2, "failed_records": 0}

        monkeypatch.setattr(
            "load.sales_module_loader.process_source_records",
            fake_process_source_records,
        )

        result = process_records_by_module(
            session=object(),
            module_name="sales_invoice",
            source_records=[{"callback_id": 1}, {"callback_id": 2}],
            transform_func=lambda rec: [rec],
            chunk_size=123,
        )

        assert result == {"processed_rows": 2, "failed_records": 0}
        assert captured["model"] is MODULE_LOADER_CONFIG["sales_invoice"]["model"]
        assert captured["allowed_columns"] == MODULE_LOADER_CONFIG["sales_invoice"]["allowed_columns"]
        assert captured["required_fields"] == MODULE_LOADER_CONFIG["sales_invoice"]["required_fields"]
        assert captured["update_columns"] == MODULE_LOADER_CONFIG["sales_invoice"]["update_columns"]
        assert captured["decimal_columns"] == MODULE_LOADER_CONFIG["sales_invoice"]["decimal_columns"]
        assert captured["context"] == "sales_invoice"
        assert captured["chunk_size"] == 123
