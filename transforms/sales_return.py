from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional


# ============================================================
# CONFIG
# ============================================================

# Untuk sales return, akun line item pada payload diasumsikan akun pendapatan asli.
# Karena ini return, maka akun tersebut didebit sebagai contra-revenue / reversal revenue.

TAX_ACCOUNT_MAP = {
    "PPN 11%": {
        "account_code": "214111",
        "coa_name": "Output VAT 11%",
    },
    "PPN 12%": {
        "account_code": "214112",
        "coa_name": "Output VAT 12%",
    },
}

DEFAULT_TAX_ACCOUNT = {
    "account_code": "214999",
    "coa_name": "Other Output Tax",
}


# ============================================================
# HELPERS
# ============================================================

def to_decimal(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def to_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def get_nested(d: Dict[str, Any], *keys: str, default=None):
    current = d
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def compute_realization(account_code: Optional[str], debit: Decimal, credit: Decimal) -> Decimal:
    """
    Default sign = debit - credit
    For groups 2,3,4,8 => reversed sign
    """
    try:
        first_digit = int(str(account_code)[0]) if account_code else None
    except (ValueError, TypeError, IndexError):
        first_digit = None

    base = debit - credit
    if first_digit in (2, 3, 4, 8):
        return -base
    return base


def parse_json_body(raw_body: Any) -> Dict[str, Any]:
    """
    Parse source DB body:
    - dict
    - JSON string
    - double-encoded JSON string
    """
    if isinstance(raw_body, dict):
        return raw_body

    if raw_body is None:
        raise ValueError("body is None")

    if not isinstance(raw_body, str):
        raise TypeError(f"body must be str or dict, got {type(raw_body)}")

    raw_body = raw_body.strip()
    if not raw_body:
        raise ValueError("body is empty")

    try:
        parsed = json.loads(raw_body)
        if isinstance(parsed, str):
            parsed = json.loads(parsed)

        if not isinstance(parsed, dict):
            raise ValueError("parsed body is not a dict")

        return parsed
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON body: {e}") from e


def get_tax_account(tax_obj: Dict[str, Any]) -> Dict[str, str]:
    tax_code = (tax_obj.get("code") or "").strip()
    tax_name = (tax_obj.get("name") or "").strip()

    if tax_code in TAX_ACCOUNT_MAP:
        return TAX_ACCOUNT_MAP[tax_code]
    if tax_name in TAX_ACCOUNT_MAP:
        return TAX_ACCOUNT_MAP[tax_name]

    return DEFAULT_TAX_ACCOUNT


def reconcile_rows(
    rows: List[Dict[str, Any]],
    source_id: str,
    tolerance: Decimal = Decimal("0.01"),
) -> None:
    total_debit = sum((to_decimal(r.get("debit")) for r in rows), Decimal("0"))
    total_credit = sum((to_decimal(r.get("credit")) for r in rows), Decimal("0"))
    diff = total_debit - total_credit

    if abs(diff) > tolerance:
        raise ValueError(
            f"unbalanced sales_return transform for source_id={source_id}: "
            f"total_debit={total_debit}, total_credit={total_credit}, diff={diff}"
        )


# ============================================================
# VALIDATION
# ============================================================

def validate_sales_return_payload(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    end_point = payload.get("end_point")
    if end_point != "sales_returns":
        raise ValueError(f"unexpected end_point: {end_point}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("payload.data must be a dict")

    required_fields = {
        "data.id": data.get("id"),
        "data.number": data.get("number"),
        "data.date": data.get("date"),
        "data.customer.name": get_nested(data, "customer", "name"),
    }

    missing = [field for field, value in required_fields.items() if value in (None, "")]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_base_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    customer = data.get("customer") or {}
    department = data.get("department") or {}
    project = data.get("project") or {}
    currency = data.get("currency") or {}
    created = data.get("created") or {}
    created_user = created.get("user") or {}
    parent_memo = data.get("parent_memo") or {}

    return {
        "date": data.get("date"),
        "type": "sales_returns",
        "ref_no": data.get("number"),
        "contact": customer.get("name"),
        "description": data.get("description"),
        "department": department.get("name"),
        "project": project.get("name"),
        "source_id": data.get("id"),
        "status": data.get("status"),
        "currency": currency.get("code") or "IDR",
        "exchange_rate": to_decimal(data.get("exchange_rate", 1)),
        "created_at": created.get("time"),
        "created_by": created_user.get("name"),
        "sales_ref_no": parent_memo.get("number"),
        "sales_date": parent_memo.get("date"),
        "sales_id": parent_memo.get("id"),
    }


# ============================================================
# MAIN TRANSFORM
# ============================================================

def transform_sales_return_from_body(raw_body: Any) -> List[Dict[str, Any]]:
    payload = parse_json_body(raw_body)
    return transform_sales_return(payload)


def transform_sales_return(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Journal pattern:
    1. Debit Sales Return / reversal revenue from line_items
    2. Debit Output VAT reversal from taxes
    3. Credit A/R from payments
    """
    validate_sales_return_payload(payload)

    data = payload["data"]
    base = normalize_base_fields(data)

    rows: List[Dict[str, Any]] = []

    # ============================================================
    # 1) DEBIT SALES RETURN / REVERSAL REVENUE
    # ============================================================
    for line in data.get("line_items", []) or []:
        dept = line.get("department") or data.get("department") or {}
        proj = line.get("project") or data.get("project") or {}
        curr = data.get("currency") or {}
        acc = line.get("account") or {}

        line_base = {
            **base,
            "department": dept.get("name"),
            "project": proj.get("name"),
            "currency": curr.get("code") or base["currency"],
            "exchange_rate": to_decimal(data.get("exchange_rate", base["exchange_rate"])),
        }

        revenue_reversal = to_decimal(
            line.get("subtotal_after_discount")
            if line.get("subtotal_after_discount") is not None
            else line.get("subtotal_before_discount")
        )

        account_code = to_str(acc.get("code"))

        if revenue_reversal != 0:
            rows.append({
                **line_base,
                "note": line.get("description") or "Sales Return",
                "debit": revenue_reversal,
                "credit": Decimal("0"),
                "realization": compute_realization(account_code, revenue_reversal, Decimal("0")),
                "account_code": account_code,
                "coa_name": acc.get("name"),
                "source_line_id": to_str(line.get("id")),
            })

        # ========================================================
        # 2) DEBIT TAX REVERSAL
        # ========================================================
        for tax in line.get("taxes", []) or []:
            tax_amount = to_decimal(tax.get("amount"))
            if tax_amount == 0:
                continue

            tax_account = get_tax_account(tax)
            tax_code = tax_account["account_code"]

            rows.append({
                **line_base,
                "note": tax.get("name") or tax.get("code") or "Tax Reversal",
                "debit": tax_amount,
                "credit": Decimal("0"),
                "realization": compute_realization(tax_code, tax_amount, Decimal("0")),
                "account_code": tax_code,
                "coa_name": tax_account["coa_name"],
                "source_line_id": f"{to_str(tax.get('id'))}_tax",
            })

    # ============================================================
    # 3) CREDIT A/R
    # ============================================================
    for pay in data.get("payments", []) or []:
        acc = pay.get("account") or {}
        payment_amount = to_decimal(pay.get("amount"))
        account_code = to_str(acc.get("code"))

        if payment_amount == 0:
            continue

        rows.append({
            **base,
            "note": "Accounts Receivable Reversal",
            "debit": Decimal("0"),
            "credit": payment_amount,
            "realization": compute_realization(account_code, Decimal("0"), payment_amount),
            "account_code": account_code,
            "coa_name": acc.get("name"),
            "source_line_id": to_str(pay.get("id")),
            "currency": get_nested(pay, "currency", "code", default=base["currency"]) or base["currency"],
            "exchange_rate": to_decimal(pay.get("exchange_rate", base["exchange_rate"])),
        })

    if not rows:
        raise ValueError(f"no rows generated for sales_return source_id={data.get('id')}")

    reconcile_rows(rows, source_id=str(data["id"]))
    return rows


# ============================================================
# OPTIONAL WRAPPER FOR SOURCE RECORD
# ============================================================

def transform_sales_return_source_record(source_record: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = transform_sales_return_from_body(source_record["body"])

    for row in rows:
        row["raw_callback_id"] = to_str(source_record.get("callback_id"))
        row["raw_created_at"] = source_record.get("created_at")

    return rows
