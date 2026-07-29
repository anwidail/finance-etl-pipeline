from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from transforms.timeutil import to_wib

from transforms.tax_master import resolve_discount_account, resolve_tax_account


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


def get_contact_name(data: Dict[str, Any]) -> Optional[str]:
    return (
        get_nested(data, "vendor", "name")
        or get_nested(data, "supplier", "name")
        or get_nested(data, "contact", "name")
    )


def compute_realization(account_code: Optional[str], debit: Decimal, credit: Decimal) -> Decimal:
    try:
        first_digit = int(str(account_code)[0]) if account_code else None
    except (ValueError, TypeError, IndexError):
        first_digit = None

    base = debit - credit
    if first_digit in (2, 3, 4, 8):
        return -base
    return base


def parse_json_body(raw_body: Any) -> Dict[str, Any]:
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
            f"unbalanced purchase_return transform for source_id={source_id}: "
            f"total_debit={total_debit}, total_credit={total_credit}, diff={diff}"
        )


def validate_purchase_return_payload(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    end_point = payload.get("end_point")
    # Production data uses the "purchases_returns" spelling; older fixtures use
    # "purchase_returns". Accept both so extraction and tests stay in sync.
    if end_point not in ("purchases_returns", "purchase_returns"):
        raise ValueError(f"unexpected end_point: {end_point}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("payload.data must be a dict")

    required_fields = {
        "data.id": data.get("id"),
        "data.number": data.get("number"),
        "data.date": data.get("date"),
        "data.vendor.name": get_contact_name(data),
    }

    missing = [field for field, value in required_fields.items() if value in (None, "")]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")


def normalize_base_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    department = data.get("department") or {}
    project = data.get("project") or {}
    currency = data.get("currency") or {}
    created = data.get("created") or {}
    created_user = created.get("user") or {}
    parent_memo = data.get("parent_memo") or {}

    return {
        "date": data.get("date"),
        "type": "purchase_returns",
        "ref_no": data.get("number"),
        "contact": get_contact_name(data),
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
        "purchase_ref_no": parent_memo.get("number"),
        "purchase_date": parent_memo.get("date"),
        "purchase_id": parent_memo.get("id"),
    }


def transform_purchase_return_from_body(raw_body: Any) -> List[Dict[str, Any]]:
    payload = parse_json_body(raw_body)
    return transform_purchase_return(payload)


def transform_purchase_return(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Journal pattern:
    1. Debit A/P reversal from payments
    2. Credit purchase reversal from line_items
    3. Credit input tax reversal from taxes
    """
    validate_purchase_return_payload(payload)

    data = payload["data"]
    base = normalize_base_fields(data)
    rows: List[Dict[str, Any]] = []

    for pay in data.get("payments", []) or []:
        acc = pay.get("account") or {}
        payable_amount = to_decimal(pay.get("amount"))
        account_code = to_str(acc.get("code"))

        if payable_amount == 0:
            continue

        rows.append({
            **base,
            "note": "Accounts Payable Reversal",
            "debit": payable_amount,
            "credit": Decimal("0"),
            "amount": compute_realization(account_code, payable_amount, Decimal("0")),
            "account_code": account_code,
            "coa_name": acc.get("name"),
            "source_line_id": to_str(pay.get("id")),
            "currency": get_nested(pay, "currency", "code", default=base["currency"]) or base["currency"],
            "exchange_rate": to_decimal(pay.get("exchange_rate", base["exchange_rate"])),
        })

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

        # The expense reversal is credited GROSS (pre-discount subtotal) and the
        # line discount is reversed with a DEBIT to W/H TA 23 — the mirror of the
        # purchase invoice, which credits it. Grossing up and debiting the same
        # discount offsets exactly, so a zero discount is a plain net reversal.
        before_amount = to_decimal(line.get("subtotal_before_discount"))
        after_amount = to_decimal(
            line.get("subtotal_after_discount")
            if line.get("subtotal_after_discount") is not None
            else line.get("subtotal_before_discount")
        )
        purchase_reversal = before_amount
        discount_amount = before_amount - after_amount
        account_code = to_str(acc.get("code"))

        if purchase_reversal != 0:
            rows.append({
                **line_base,
                "note": line.get("description") or "Purchase Return",
                "debit": Decimal("0"),
                "credit": purchase_reversal,
                "amount": compute_realization(account_code, Decimal("0"), purchase_reversal),
                "account_code": account_code,
                "coa_name": acc.get("name"),
                "source_line_id": to_str(line.get("id")),
            })

        if discount_amount != 0:
            disc_code, disc_coa_name = resolve_discount_account("purchase")
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

        for tax in line.get("taxes", []) or []:
            tax_amount = to_decimal(tax.get("amount"))
            if tax_amount == 0:
                continue

            tax_code, tax_coa_name = resolve_tax_account(tax, "purchase")

            rows.append({
                **line_base,
                "note": tax.get("name") or tax.get("code") or "Input Tax Reversal",
                "debit": Decimal("0"),
                "credit": tax_amount,
                "amount": compute_realization(tax_code, Decimal("0"), tax_amount),
                "account_code": tax_code,
                "coa_name": tax_coa_name,
                # tax.id is the tax-type id (shared across line items), so prefix
                # with the line id to keep (source_id, source_line_id) unique.
                "source_line_id": f"{to_str(line.get('id'))}_{to_str(tax.get('id'))}_tax",
            })

    if not rows:
        raise ValueError(f"no rows generated for purchase_return source_id={data.get('id')}")

    reconcile_rows(rows, source_id=str(data["id"]))
    return rows


def transform_purchase_return_source_record(source_record: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = transform_purchase_return_from_body(source_record["body"])

    for row in rows:
        row["raw_callback_id"] = to_str(source_record.get("callback_id"))
        row["raw_created_at"] = source_record.get("created_at")

    return rows
