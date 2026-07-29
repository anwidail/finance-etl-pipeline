from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from transforms.timeutil import to_wib

from transforms.tax_master import resolve_discount_account, resolve_tax_account


# ============================================================
# HELPERS
# ============================================================

def to_decimal(value: Any, default: str = "0") -> Decimal:
    """
    Safely convert any numeric-like value to Decimal.
    """
    if value is None or value == "":
        return Decimal(default)

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def to_str(value: Any) -> Optional[str]:
    """
    Convert to string safely, preserving None.
    """
    if value is None:
        return None
    return str(value)


def compute_realization(account_code: Optional[str], debit: Decimal, credit: Decimal) -> Decimal:
    """
    Calculate realization based on account code group.

    Rule:
    - default = debit - credit
    - for account groups 2,3,4,8 => reversed sign
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
    Parse callback body from source DB.

    Handles:
    - dict مباشرة / already parsed
    - normal JSON string
    - double-encoded JSON string

    Examples:
    1. '{"end_point":"sales_invoices","data":{...}}'
    2. '"{\\"end_point\\":\\"sales_invoices\\",\\"data\\":{...}}"'
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

        # handle double-encoded JSON
        if isinstance(parsed, str):
            parsed = json.loads(parsed)

        if not isinstance(parsed, dict):
            raise ValueError("parsed body is not a dict")

        return parsed

    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON body: {e}") from e


def validate_sales_invoice_payload(payload: Dict[str, Any]) -> None:
    """
    Validate required fields for sales invoice payload.
    Raise ValueError if invalid.
    """
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    end_point = payload.get("end_point")
    if end_point != "sales_invoices":
        raise ValueError(f"unexpected end_point: {end_point}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("payload.data must be a dict")

    required_fields = {
        "data.id": data.get("id"),
        "data.number": data.get("number"),
        "data.date": data.get("date"),
        "data.customer.name": (data.get("customer") or {}).get("name"),
    }

    missing = [field for field, value in required_fields.items() if value in (None, "")]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")


def normalize_base_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Common fields copied into every generated row.
    """
    customer = data.get("customer") or {}
    department = data.get("department") or {}
    project = data.get("project") or {}
    currency = data.get("currency") or {}
    created = data.get("created") or {}
    created_user = created.get("user") or {}

    return {
        "date": data.get("date"),
        "type": "sales_invoices",
        "ref_no": data.get("number"),
        "contact": customer.get("name"),
        "description": data.get("description"),
        "dept_code": department.get("code"),
        "department": department.get("name"),
        "project": project.get("name"),
        "source_id": data.get("id"),
        "status": data.get("status"),
        "currency": currency.get("code") or "IDR",
        "exchange_rate": to_decimal(data.get("exchange_rate", 1)),
        "created_at": to_wib(created.get("time")),
        "created_by": created_user.get("name"),
    }


def reconcile_invoice_rows(
    rows: List[Dict[str, Any]],
    source_id: str,
    tolerance: Decimal = Decimal("0.01"),
) -> None:
    """
    Ensure transformed rows are balanced:
    total debit must equal total credit.
    """
    total_debit = sum((to_decimal(r.get("debit")) for r in rows), Decimal("0"))
    total_credit = sum((to_decimal(r.get("credit")) for r in rows), Decimal("0"))
    diff = total_debit - total_credit

    if abs(diff) > tolerance:
        raise ValueError(
            f"unbalanced transform for source_id={source_id}: "
            f"total_debit={total_debit}, total_credit={total_credit}, diff={diff}"
        )


# ============================================================
# MAIN TRANSFORM
# ============================================================

def transform_sales_invoice_from_body(raw_body: Any) -> List[Dict[str, Any]]:
    """
    Full entry point:
    parse raw JSON body from source DB -> validate -> transform.

    Returns list of line-level rows for finance_db.sales_invoice
    """
    payload = parse_json_body(raw_body)
    return transform_sales_invoice(payload)


def transform_sales_invoice(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Transform one sales_invoices payload into line-level accounting rows.

    Output pattern:
    1. Debit A/R from payments
    2. Credit revenue from line_items
    3. Credit tax from line_items[].taxes
    """
    validate_sales_invoice_payload(payload)

    data = payload["data"]
    base = normalize_base_fields(data)

    rows: List[Dict[str, Any]] = []

    # ============================================================
    # 1) DEBIT SECTION — Accounts Receivable
    # ============================================================
    for pay in data.get("payments", []) or []:
        acc = pay.get("account") or {}

        debit = to_decimal(pay.get("amount"))
        credit = Decimal("0")
        account_code = to_str(acc.get("code"))
        exchange_rate = to_decimal(pay.get("exchange_rate", base["exchange_rate"]))

        if debit == 0:
            continue

        rows.append({
            **base,
            "note": "Accounts Receivable",
            "debit": debit,
            "credit": credit,
            "amount": compute_realization(account_code, debit, credit),
            "account_code": account_code,
            "coa_name": acc.get("name"),
            "source_line_id": to_str(pay.get("id")),
            "currency": (pay.get("currency") or {}).get("code") or base["currency"],
            "exchange_rate": exchange_rate,
        })

    # ============================================================
    # 2) CREDIT SECTION — Revenue and Tax
    # ============================================================
    for line in data.get("line_items", []) or []:
        dept = line.get("department") or data.get("department") or {}
        proj = line.get("project") or data.get("project") or {}
        curr = line.get("currency") or data.get("currency") or {}
        acc = line.get("account") or {}

        line_base = {
            **base,
            "dept_code": dept.get("code"),
            "department": dept.get("name"),
            "project": proj.get("name"),
            "currency": curr.get("code") or base["currency"],
            "exchange_rate": to_decimal(line.get("exchange_rate", base["exchange_rate"])),
        }

        # Revenue is credited GROSS (at the pre-discount subtotal); the line
        # discount is posted separately to Pre TA 23 as a debit. Grossing up and
        # debiting the same discount offsets exactly, so a zero discount leaves
        # the entry identical to a plain net posting.
        before_amount = to_decimal(line.get("subtotal_before_discount"))
        after_amount = to_decimal(
            line.get("subtotal_after_discount")
            if line.get("subtotal_after_discount") is not None
            else line.get("subtotal_before_discount")
        )
        revenue_amount = before_amount
        discount_amount = before_amount - after_amount

        if revenue_amount != 0:
            debit = Decimal("0")
            credit = revenue_amount
            account_code = to_str(acc.get("code"))

            rows.append({
                **line_base,
                "note": line.get("description") or "Revenue",
                "debit": debit,
                "credit": credit,
                "amount": compute_realization(account_code, debit, credit),
                "account_code": account_code,
                "coa_name": acc.get("name"),
                "source_line_id": to_str(line.get("id")),
            })

        if discount_amount != 0:
            disc_code, disc_coa_name = resolve_discount_account("sales")
            rows.append({
                **line_base,
                "note": "Discount",
                "debit": discount_amount,
                "credit": Decimal("0"),
                "amount": compute_realization(disc_code, discount_amount, Decimal("0")),
                "account_code": disc_code,
                "coa_name": disc_coa_name,
                "source_line_id": f"{to_str(line.get('id'))}_disc",
            })

        # Tax
        for tax in line.get("taxes", []) or []:
            tax_amount = to_decimal(tax.get("amount"))
            if tax_amount == 0:
                continue

            tax_code, tax_coa_name = resolve_tax_account(tax, "sales")

            debit = Decimal("0")
            credit = tax_amount

            rows.append({
                **line_base,
                "note": tax.get("name") or tax.get("code") or "Tax",
                "debit": debit,
                "credit": credit,
                "amount": compute_realization(tax_code, debit, credit),
                "account_code": tax_code,
                "coa_name": tax_coa_name,
                # tax.id is the tax-type id (shared across line items), so prefix
                # with the line id to keep (source_id, source_line_id) unique.
                "source_line_id": f"{to_str(line.get('id'))}_{to_str(tax.get('id'))}_tax",
            })

    if not rows:
        raise ValueError(f"no rows generated for sales_invoice source_id={data.get('id')}")

    reconcile_invoice_rows(rows, source_id=str(data["id"]))

    return rows

# ============================================================
# OPTIONAL WRAPPER FOR SOURCE RECORD
# ============================================================

def transform_sales_invoice_source_record(source_record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    source_record example:
    {
        "callback_id": 12345,
        "body": "...json string...",
        "created_at": "2026-03-01 10:00:00"
    }
    """
    rows = transform_sales_invoice_from_body(source_record["body"])

    for row in rows:
        row["raw_callback_id"] = to_str(source_record.get("callback_id"))
        row["raw_created_at"] = source_record.get("created_at")

    return rows