from __future__ import annotations

from models.finance import SalesInvoice, ReceivablePayment, SalesReturn


COMMON_COLUMNS = [
    "date",
    "type",
    "ref_no",
    "contact",
    "description",
    "note",
    "department",
    "project",
    "debit",
    "credit",
    "realization",
    "account_code",
    "coa_name",
    "source_id",
    "source_line_id",
    "currency",
    "exchange_rate",
    "status",
    "created_at",
    "created_by",
]

SALES_REFERENCE_COLUMNS = [
    "sales_ref_no",
    "sales_date",
    "sales_id",
]

DECIMAL_COLUMNS = [
    "debit",
    "credit",
    "realization",
    "exchange_rate",
]

COMMON_REQUIRED_FIELDS = [
    "type",
    "ref_no",
    "source_id",
    "source_line_id",
]

COMMON_UPDATE_COLUMNS = [
    "date",
    "type",
    "ref_no",
    "contact",
    "description",
    "note",
    "department",
    "project",
    "debit",
    "credit",
    "realization",
    "account_code",
    "coa_name",
    "currency",
    "exchange_rate",
    "status",
    "created_at",
    "created_by",
]

SALES_UPDATE_COLUMNS = COMMON_UPDATE_COLUMNS + SALES_REFERENCE_COLUMNS


MODULE_LOADER_CONFIG = {
    "sales_invoice": {
        "model": SalesInvoice,
        "allowed_columns": COMMON_COLUMNS,
        "required_fields": COMMON_REQUIRED_FIELDS,
        "update_columns": COMMON_UPDATE_COLUMNS,
        "decimal_columns": DECIMAL_COLUMNS,
        "context": "sales_invoice",
    },
    "receivable_payment": {
        "model": ReceivablePayment,
        "allowed_columns": COMMON_COLUMNS + SALES_REFERENCE_COLUMNS,
        "required_fields": COMMON_REQUIRED_FIELDS,
        "update_columns": SALES_UPDATE_COLUMNS,
        "decimal_columns": DECIMAL_COLUMNS,
        "context": "receivable_payment",
    },
    "sales_return": {
        "model": SalesReturn,
        "allowed_columns": COMMON_COLUMNS + SALES_REFERENCE_COLUMNS,
        "required_fields": COMMON_REQUIRED_FIELDS,
        "update_columns": SALES_UPDATE_COLUMNS,
        "decimal_columns": DECIMAL_COLUMNS,
        "context": "sales_return",
    },
}