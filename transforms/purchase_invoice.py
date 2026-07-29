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
            f"unbalanced purchase_invoice transform for source_id={source_id}: "
            f"total_debit={total_debit}, total_credit={total_credit}, diff={diff}"
        )


def validate_purchase_invoice_payload(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    end_point = payload.get("end_point")
    # Production data uses the "purchases_invoices" spelling; older fixtures use
    # "purchase_invoices". Accept both so extraction and tests stay in sync.
    if end_point not in ("purchases_invoices", "purchase_invoices"):
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

    return {
        "date": data.get("date"),
        "type": "purchase_invoices",
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
    }


def transform_purchase_invoice_from_body(raw_body: Any) -> List[Dict[str, Any]]:
    payload = parse_json_body(raw_body)
    return transform_purchase_invoice(payload)


def transform_purchase_invoice(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Journal pattern:
    1. Debit expense / inventory from line_items
    2. Debit input tax from line_items[].taxes
    3. Credit A/P from payments
    """
    validate_purchase_invoice_payload(payload)

    data = payload["data"]
    base = normalize_base_fields(data)
    rows: List[Dict[str, Any]] = []

    for line in data.get("line_items", []) or []:
        dept = line.get("department") or data.get("department") or {}
        proj = line.get("project") or data.get("project") or {}
        curr = line.get("currency") or data.get("currency") or {}
        acc = line.get("account") or {}

        line_xr = to_decimal(line.get("exchange_rate", base["exchange_rate"]))
        line_base = {
            **base,
            "dept_code": dept.get("code"),
            "department": dept.get("name"),
            "project": proj.get("name"),
            "currency": curr.get("code") or base["currency"],
            "exchange_rate": line_xr,
        }

        # Line-item amounts are in the transaction (origin) currency, while the
        # payment side below uses the base-currency `amount`. Convert the debit
        # side to base currency so the journal balances. For base-currency
        # invoices the exchange rate is 1, leaving the value unchanged.
        #
        # The expense is booked GROSS (at the pre-discount subtotal) and the line
        # discount is posted separately to W/H TA 23 as a credit. Grossing up and
        # crediting the same discount offsets exactly, so a zero discount leaves
        # the entry identical to a plain net posting.
        before_amount = to_decimal(line.get("subtotal_before_discount"))
        after_amount = to_decimal(
            line.get("subtotal_after_discount")
            if line.get("subtotal_after_discount") is not None
            else line.get("subtotal_before_discount")
        )
        purchase_amount = before_amount * line_xr
        discount_amount = (before_amount - after_amount) * line_xr
        account_code = to_str(acc.get("code"))

        if purchase_amount != 0:
            rows.append({
                **line_base,
                "note": line.get("description") or "Purchase",
                "debit": purchase_amount,
                "credit": Decimal("0"),
                "amount": compute_realization(account_code, purchase_amount, Decimal("0")),
                "account_code": account_code,
                "coa_name": acc.get("name"),
                "source_line_id": to_str(line.get("id")),
            })

        if discount_amount != 0:
            disc_code, disc_coa_name = resolve_discount_account("purchase")
            rows.append({
                **line_base,
                "note": "Discount",
                "debit": Decimal("0"),
                "credit": discount_amount,
                "amount": compute_realization(disc_code, Decimal("0"), discount_amount),
                "account_code": disc_code,
                "coa_name": disc_coa_name,
                "source_line_id": f"{to_str(line.get('id'))}_disc",
            })

        for tax in line.get("taxes", []) or []:
            tax_amount = to_decimal(tax.get("amount")) * line_xr
            if tax_amount == 0:
                continue

            tax_code, tax_coa_name = resolve_tax_account(tax, "purchase")

            rows.append({
                **line_base,
                "note": tax.get("name") or tax.get("code") or "Input Tax",
                "debit": tax_amount,
                "credit": Decimal("0"),
                "amount": compute_realization(tax_code, tax_amount, Decimal("0")),
                "account_code": tax_code,
                "coa_name": tax_coa_name,
                # tax.id is the tax-type id (e.g. shared "PPN 11%"), so it repeats
                # across line items. Prefix with the line id to keep the upsert key
                # (source_id, source_line_id) unique within an invoice.
                "source_line_id": f"{to_str(line.get('id'))}_{to_str(tax.get('id'))}_tax",
            })

    # Other charges (freight, stamp duty, purchase VAT, etc.) are carried in the
    # `others` array and are included in the payable total, so they need their own
    # debit row. The `amount` field is already in base currency, like payments.
    for other in data.get("others", []) or []:
        acc = other.get("account") or {}
        other_amount = to_decimal(other.get("amount"))
        account_code = to_str(acc.get("code"))

        if other_amount == 0:
            continue

        rows.append({
            **base,
            "note": acc.get("name") or "Other Charge",
            "debit": other_amount,
            "credit": Decimal("0"),
            "amount": compute_realization(account_code, other_amount, Decimal("0")),
            "account_code": account_code,
            "coa_name": acc.get("name"),
            "source_line_id": f"{to_str(other.get('id'))}_other",
            "currency": get_nested(other, "currency", "code", default=base["currency"]) or base["currency"],
            "exchange_rate": to_decimal(other.get("exchange_rate", base["exchange_rate"])),
        })

    for pay in data.get("payments", []) or []:
        acc = pay.get("account") or {}
        payable_amount = to_decimal(pay.get("amount"))
        account_code = to_str(acc.get("code"))

        if payable_amount == 0:
            continue

        rows.append({
            **base,
            "note": "Accounts Payable",
            "debit": Decimal("0"),
            "credit": payable_amount,
            "amount": compute_realization(account_code, Decimal("0"), payable_amount),
            "account_code": account_code,
            "coa_name": acc.get("name"),
            "source_line_id": to_str(pay.get("id")),
            "currency": get_nested(pay, "currency", "code", default=base["currency"]) or base["currency"],
            "exchange_rate": to_decimal(pay.get("exchange_rate", base["exchange_rate"])),
        })

    if not rows:
        raise ValueError(f"no rows generated for purchase_invoice source_id={data.get('id')}")

    reconcile_rows(rows, source_id=str(data["id"]))
    return rows


def transform_purchase_invoice_source_record(source_record: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = transform_purchase_invoice_from_body(source_record["body"])

    for row in rows:
        row["raw_callback_id"] = to_str(source_record.get("callback_id"))
        row["raw_created_at"] = source_record.get("created_at")

    return rows
