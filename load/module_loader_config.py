from __future__ import annotations

from models.finance import (
    CashIn,
    CashOut,
    ManualJournal,
    PayablePayment,
    PurchaseInvoice,
    PurchaseReturn,
    ReceivablePayment,
    SalesInvoice,
    SalesReturn,
)


COMMON_COLUMNS = [
    "date",
    "type",
    "ref_no",
    "contact",
    "description",
    "note",
    "dept_code",
    "department",
    "project",
    "debit",
    "credit",
    "amount",
    "account_code",
    "coa_name",
    "source_id",
    "source_line_id",
    "currency",
    "original_currency",
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

PURCHASE_REFERENCE_COLUMNS = [
    "purchase_ref_no",
    "purchase_date",
    "purchase_id",
]

DECIMAL_COLUMNS = [
    "debit",
    "credit",
    "amount",
    "original_currency",
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
    # Kept next to `department`: a document re-posted under a different
    # department must refresh both halves, or the code goes stale against the
    # name it sits beside.
    "dept_code",
    "department",
    "project",
    "debit",
    "credit",
    "amount",
    "account_code",
    "coa_name",
    "currency",
    "original_currency",
    "exchange_rate",
    "status",
    "created_at",
    "created_by",
]

SALES_UPDATE_COLUMNS = COMMON_UPDATE_COLUMNS + SALES_REFERENCE_COLUMNS
PURCHASE_UPDATE_COLUMNS = COMMON_UPDATE_COLUMNS + PURCHASE_REFERENCE_COLUMNS


# --- Unified general ledger (gl) ---
# The gl table receives every module's rows. `module` and `ref_key` are added by
# the gl loader; `ref_key` is part of the business key so it is never updated.
GL_ALLOWED_COLUMNS = COMMON_COLUMNS + ["module", "ref_key"]
GL_REQUIRED_FIELDS = ["type", "ref_no", "source_id", "module"]
GL_UPDATE_COLUMNS = COMMON_UPDATE_COLUMNS + ["module"]
# gl business key spans modules, so replace-by-document must match on (type, source_id).
GL_DELETE_SCOPE_COLUMNS = ["type", "source_id"]


MODULE_LOADER_CONFIG = {
    "manual_journal": {
        "model": ManualJournal,
        "allowed_columns": COMMON_COLUMNS,
        "required_fields": COMMON_REQUIRED_FIELDS,
        "update_columns": COMMON_UPDATE_COLUMNS,
        "decimal_columns": DECIMAL_COLUMNS,
        "context": "manual_journal",
    },
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
    "purchase_invoice": {
        "model": PurchaseInvoice,
        "allowed_columns": COMMON_COLUMNS,
        "required_fields": COMMON_REQUIRED_FIELDS,
        "update_columns": COMMON_UPDATE_COLUMNS,
        "decimal_columns": DECIMAL_COLUMNS,
        "context": "purchase_invoice",
    },
    "payable_payment": {
        "model": PayablePayment,
        "allowed_columns": COMMON_COLUMNS + PURCHASE_REFERENCE_COLUMNS,
        "required_fields": COMMON_REQUIRED_FIELDS,
        "update_columns": PURCHASE_UPDATE_COLUMNS,
        "decimal_columns": DECIMAL_COLUMNS,
        "context": "payable_payment",
    },
    "purchase_return": {
        "model": PurchaseReturn,
        "allowed_columns": COMMON_COLUMNS + PURCHASE_REFERENCE_COLUMNS,
        "required_fields": COMMON_REQUIRED_FIELDS,
        "update_columns": PURCHASE_UPDATE_COLUMNS,
        "decimal_columns": DECIMAL_COLUMNS,
        "context": "purchase_return",
    },
    "cash_in": {
        "model": CashIn,
        "allowed_columns": COMMON_COLUMNS,
        "required_fields": COMMON_REQUIRED_FIELDS,
        "update_columns": COMMON_UPDATE_COLUMNS,
        "decimal_columns": DECIMAL_COLUMNS,
        "context": "cash_in",
    },
    "cash_out": {
        "model": CashOut,
        "allowed_columns": COMMON_COLUMNS,
        "required_fields": COMMON_REQUIRED_FIELDS,
        "update_columns": COMMON_UPDATE_COLUMNS,
        "decimal_columns": DECIMAL_COLUMNS,
        "context": "cash_out",
    },
}
