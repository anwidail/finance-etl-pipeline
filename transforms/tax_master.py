from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


# ============================================================
# TAX MASTER
# ============================================================
# Single source of truth for tax -> GL account mapping, keyed by the tax `name`
# exactly as it appears in the source system (irregular spacing is intentional —
# do not "fix" it, it must match the raw name). Each entry carries both the
# purchase-side and sales-side account so the purchase and sales invoice
# transforms resolve from the same table.
#
# `tax_rate` is informational only. Journal amounts always come from the source
# `tax.amount` (which is signed — withholding arrives negative), so we never
# recompute them from the rate and never diverge from the source rounding.
TAX_MASTER = {
    "PPN 11%": {
        "tax_rate": 11,
        "purchase_account_code": "212412",
        "purchase_account_name": "Purchase VAT",
        "sales_account_code": "212411",
        "sales_account_name": "Sales VAT",
    },
    # Legacy 10% VAT (code "PPN", pre-2022 rate). Same VAT accounts as PPN 11%.
    "PPN": {
        "tax_rate": 10,
        "purchase_account_code": "212412",
        "purchase_account_name": "Purchase VAT",
        "sales_account_code": "212411",
        "sales_account_name": "Sales VAT",
    },
    "PPh 23-2": {
        "tax_rate": -2,
        "purchase_account_code": "212423",
        "purchase_account_name": "W/H TA 23 (Supplier/Vendor)",
        "sales_account_code": "114411",
        "sales_account_name": "Pre TA 23 (Cust./Client)",
    },
    "PPN  1.2%": {
        "tax_rate": 1.2,
        "purchase_account_code": "212412",
        "purchase_account_name": "Purchase VAT",
        "sales_account_code": "212411",
        "sales_account_name": "Sales VAT",
    },
    "PPN 2 1,1%": {
        "tax_rate": 1.1,
        "purchase_account_code": "212412",
        "purchase_account_name": "Purchase VAT",
        "sales_account_code": "212411",
        "sales_account_name": "Sales VAT",
    },
}


# ============================================================
# DISCOUNT
# ============================================================
# Line-level discounts are booked as a withholding-style adjustment to the same
# PPh 23 accounts used by the tax master: on a purchase the discount is a credit
# to W/H TA 23 (a payable to the tax office), on a sale it is a debit to Pre TA
# 23 (a prepaid-tax asset). The line's revenue/expense side is grossed up to the
# pre-discount subtotal so the entry still balances exactly.
DISCOUNT_ACCOUNT = {
    "purchase": {"account_code": "212423", "coa_name": "W/H TA 23 (Supplier/Vendor)"},
    "sales": {"account_code": "114411", "coa_name": "Pre TA 23 (Cust./Client)"},
}


def resolve_discount_account(side: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve (account_code, coa_name) for a line discount by side."""
    acc = DISCOUNT_ACCOUNT.get(side, {})
    return acc.get("account_code"), acc.get("coa_name")


def resolve_tax_account(tax_obj: Dict[str, Any], side: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve (account_code, coa_name) for a tax object and side.

    `side` is "purchase" or "sales". TAX_MASTER is keyed on the short tax key
    that the source sends in `code` (e.g. "PPN 11%") — the `name` there is the
    long descriptive form ("Pajak Pertambahan Nilai 11%") — so we match on
    `code` first, then fall back to `name` for taxes that only carry the key in
    `name` (e.g. "PPN 2 1,1%").

    When nothing matches, account_code is None (the row still loads and stays
    balanced — account_code is not a required field) and coa_name falls back to
    the tax name, then the code.
    """
    code = tax_obj.get("code") or None
    name = tax_obj.get("name") or None
    tax_map = TAX_MASTER.get(code) or TAX_MASTER.get(name) or {}
    account_code = tax_map.get(f"{side}_account_code")
    coa_name = tax_map.get(f"{side}_account_name") or name or code
    return account_code, coa_name
