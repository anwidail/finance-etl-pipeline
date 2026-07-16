from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from transforms.timeutil import to_wib

from transforms.forex import (
    BANK_CHARGE_CODE,
    BANK_CHARGE_NAME,
    forex_account,
    is_fx_settlement,
)
from transforms.tax_master import resolve_discount_account


# ============================================================
# CONFIG
# ============================================================

# Withholding tax the customer deducts from our receivable (creditable prepaid
# tax asset on the sales side). coa 1144-11-000 -> normalized gl code 114411.
WHT_ACCOUNT_CODE = "114411"
WHT_ACCOUNT_NAME = "Pre TA 23 (Cust./Client)"

DEFAULT_CASH_ACCOUNT = {
    "account_code": "111999",
    "coa_name": "Cash/Bank Clearing",
}

# Bank admin fee the bank deducts from a receipt (6924, see transforms.forex).
# Only auto-post the balancing residual up to this size; a larger gap is a real
# discrepancy that must surface as an error instead of being masked.
BANK_CHARGE_ACCOUNT_CODE = BANK_CHARGE_CODE
BANK_CHARGE_ACCOUNT_NAME = BANK_CHARGE_NAME
BANK_CHARGE_MAX_ABS = Decimal("50000")


# ============================================================
# HELPERS
# ============================================================

def to_decimal(value: Any, default: str = "0") -> Decimal:
    """
    Safely convert value to Decimal.
    """
    if value is None or value == "":
        return Decimal(default)

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def to_str(value: Any) -> Optional[str]:
    """
    Convert any value to string safely.
    """
    if value is None:
        return None
    return str(value)


def first_nonzero_amount(*values: Any) -> Decimal:
    """
    Return the first value that converts to a non-zero Decimal.

    The source puts the cleared A/R amount in `payable.amount`, while
    `payment.amount` is present but always 0. A plain "first not-None" fallback
    would wrongly pick that 0 (0 is not None) and skip the A/R line, unbalancing
    the journal — so we skip zeros too.
    """
    for value in values:
        amount = to_decimal(value)
        if amount != 0:
            return amount
    return Decimal("0")


def get_nested(d: Dict[str, Any], *keys: str, default=None):
    """
    Safe nested getter for dict.
    """
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
    Accounting sign convention:
    - default = debit - credit
    - groups 2,3,4,8 reversed
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
    Parse source DB body that may be:
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


def reconcile_rows(
    rows: List[Dict[str, Any]],
    source_id: str,
    tolerance: Decimal = Decimal("0.01"),
) -> None:
    """
    Ensure total debit equals total credit.
    """
    total_debit = sum((to_decimal(r.get("debit")) for r in rows), Decimal("0"))
    total_credit = sum((to_decimal(r.get("credit")) for r in rows), Decimal("0"))
    diff = total_debit - total_credit

    if abs(diff) > tolerance:
        raise ValueError(
            f"unbalanced receivable_payment transform for source_id={source_id}: "
            f"total_debit={total_debit}, total_credit={total_credit}, diff={diff}"
        )


# ============================================================
# VALIDATION
# ============================================================

def validate_receivable_payment_payload(payload: Dict[str, Any]) -> None:
    """
    Validate required fields.
    """
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    end_point = payload.get("end_point")
    if end_point != "receivable_payments":
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
    """
    Common fields copied into every generated row.
    Uses first invoice as default sales reference.
    """
    customer = data.get("customer") or {}
    department = data.get("department") or {}
    project = data.get("project") or {}
    currency = data.get("currency") or {}
    created = data.get("created") or {}
    created_user = created.get("user") or {}

    line_items = data.get("line_items", []) or []
    first_invoice = {}
    if line_items:
        first_invoice = (line_items[0] or {}).get("invoice", {}) or {}

    return {
        "date": data.get("date"),
        "type": "receivable_payments",
        "ref_no": data.get("number"),
        "contact": customer.get("name"),
        "description": data.get("description"),
        "department": department.get("name"),
        "project": project.get("name"),
        "source_id": data.get("id"),
        "status": data.get("status"),
        "currency": currency.get("code") or "IDR",
        "exchange_rate": to_decimal(data.get("exchange_rate", 1)),
        "original_currency": ((data.get("cash") or {}).get("currency") or {}).get("code") or (data.get("currency") or {}).get("code") or "IDR",
        "created_at": to_wib(created.get("time")),
        "created_by": created_user.get("name"),
        "sales_ref_no": first_invoice.get("number"),
        "sales_date": first_invoice.get("date"),
        "sales_id": first_invoice.get("id"),
    }


# ============================================================
# MAIN TRANSFORM
# ============================================================

def transform_receivable_payment_from_body(raw_body: Any) -> List[Dict[str, Any]]:
    """
    Full entry point:
    parse raw JSON body -> validate -> transform
    """
    payload = parse_json_body(raw_body)
    return transform_receivable_payment(payload)


def transform_receivable_payment(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Transform one receivable_payments payload into line-level rows.

    Journal pattern:
    1. Debit Cash/Bank from data.cash
    2. Debit Other expense if data.others.amount is negative
    3. Credit Other income if data.others.amount is positive
    4. Debit Discount from line_items[].discount.amount
    5. Credit A/R from line_items[].payment.amount
    """
    validate_receivable_payment_payload(payload)

    data = payload["data"]
    base = normalize_base_fields(data)

    rows: List[Dict[str, Any]] = []

    # ============================================================
    # 1) DEBIT CASH / BANK
    # ============================================================
    cash = data.get("cash", {}) or {}
    cash_acc = cash.get("account", {}) or {}

    # Foreign-currency settlement: the exchange difference is a realized forex
    # gain/loss rather than a bank charge, so reclassify it below.
    fx = is_fx_settlement(cash)

    cash_amount = to_decimal(cash.get("amount"))
    cash_code = to_str(cash_acc.get("code"))
    cash_name = cash_acc.get("name") or DEFAULT_CASH_ACCOUNT["coa_name"]

    if cash_amount != 0:
        rows.append({
            **base,
            "note": "Cash Receipt",
            "debit": cash_amount,
            "credit": Decimal("0"),
            "amount": compute_realization(cash_code, cash_amount, Decimal("0")),
            "account_code": cash_code or DEFAULT_CASH_ACCOUNT["account_code"],
            "coa_name": cash_name,
            "source_line_id": to_str(get_nested(cash, "account", "id")) or "cash",
            "currency": get_nested(cash, "currency", "code", default=base["currency"]) or base["currency"],
            "exchange_rate": to_decimal(cash.get("exchange_rate", base["exchange_rate"])),
        })

    # ============================================================
    # 2) OTHERS
    # negative amount -> debit expense
    # positive amount -> credit other income / liability reduction
    # ============================================================
    for other in data.get("others", []) or []:
        other_amount = to_decimal(other.get("amount"))
        if other_amount == 0:
            continue

        other_acc = other.get("account", {}) or {}
        other_code = to_str(other_acc.get("code"))
        other_name = other_acc.get("name")
        other_currency = get_nested(other, "currency", "code", default=base["currency"]) or base["currency"]
        other_exchange_rate = to_decimal(other.get("exchange_rate", base["exchange_rate"]))

        if other_amount < 0:
            debit = abs(other_amount)
            credit = Decimal("0")
            note = "Other Charge"
        else:
            debit = Decimal("0")
            credit = other_amount
            note = "Other Adjustment"

        # On an FX settlement, an exchange difference the source carries on the
        # Bank Administrative account is really a realized forex gain/loss.
        if fx and other_code == BANK_CHARGE_CODE:
            other_code, other_name = forex_account(is_debit=debit > 0)
            note = other_name

        rows.append({
            **base,
            "note": note,
            "debit": debit,
            "credit": credit,
            "amount": compute_realization(other_code, debit, credit),
            "account_code": other_code,
            "coa_name": other_name,
            "source_line_id": to_str(other.get("id")),
            "currency": other_currency,
            "exchange_rate": other_exchange_rate,
        })

    # ============================================================
    # 3) LINE ITEMS
    # discount + credit A/R
    # ============================================================
    for line in data.get("line_items", []) or []:
        inv = line.get("invoice", {}) or {}
        ar_acc = inv.get("account", {}) or {}

        invoice_no = inv.get("number")
        invoice_date = inv.get("date")
        invoice_id = inv.get("id")

        dept = line.get("department") or data.get("department") or {}
        proj = line.get("project") or data.get("project") or {}

        line_currency = get_nested(line, "currency", "code", default=base["currency"]) or base["currency"]
        line_exchange_rate = to_decimal(line.get("exchange_rate", base["exchange_rate"]))

        line_base = {
            **base,
            "department": dept.get("name"),
            "project": proj.get("name"),
            "currency": line_currency,
            "exchange_rate": line_exchange_rate,
            "sales_ref_no": invoice_no,
            "sales_date": invoice_date,
            "sales_id": invoice_id,
        }

        # 3a) Debit Discount
        discount = line.get("discount", {}) or {}
        discount_amount = to_decimal(discount.get("amount"))

        if discount_amount != 0:
            disc_code, disc_coa_name = resolve_discount_account("sales")
            rows.append({
                **line_base,
                "note": "Sales Discount",
                "debit": discount_amount,
                "credit": Decimal("0"),
                "amount": compute_realization(disc_code, discount_amount, Decimal("0")),
                "account_code": disc_code,
                "coa_name": disc_coa_name,
                "source_line_id": f"{to_str(line.get('id'))}_discount",
            })

        # 3b/3c) Settle A/R by the amount ACTUALLY paid this time. Payments can be
        # partial — one invoice may be paid over several installments — so we must
        # NOT credit the full invoice value (payable.amount, a reference only).
        # Amount settled this payment = net cash for the line + discount taken +
        # tax the customer withheld. That equals the sum of the debit lines, so
        # the entry balances.
        payable_amt = to_decimal(get_nested(line, "payable", "amount"))
        payment_amt = to_decimal(get_nested(line, "payment", "amount"))
        net_cash = to_decimal(line.get("amount"))

        if net_cash == 0:
            # No explicit per-line net cash -> full settlement of the line's gross.
            ar_settled = first_nonzero_amount(payable_amt, payment_amt)
            wht_amount = Decimal("0")
        else:
            # payment.amount is the withholding tax only when it is a distinct
            # component (payment == payable just duplicates the gross -> no tax).
            wht_amount = payment_amt if (payment_amt != 0 and payment_amt != payable_amt) else Decimal("0")
            ar_settled = net_cash + discount_amount + wht_amount

        ar_code = to_str(ar_acc.get("code"))

        if ar_settled != 0:
            rows.append({
                **line_base,
                "note": "Accounts Receivable (Payment)",
                "debit": Decimal("0"),
                "credit": ar_settled,
                "amount": compute_realization(ar_code, Decimal("0"), ar_settled),
                "account_code": ar_code,
                "coa_name": ar_acc.get("name"),
                "source_line_id": to_str(line.get("id")),
            })

        if wht_amount != 0:
            rows.append({
                **line_base,
                "note": "Withholding Tax (PPh 23)",
                "debit": wht_amount,
                "credit": Decimal("0"),
                "amount": compute_realization(WHT_ACCOUNT_CODE, wht_amount, Decimal("0")),
                "account_code": WHT_ACCOUNT_CODE,
                "coa_name": WHT_ACCOUNT_NAME,
                "source_line_id": f"{to_str(line.get('id'))}_wht",
            })

    if not rows:
        raise ValueError(f"no rows generated for receivable_payment source_id={data.get('id')}")

    # Absorb the small balancing residual: on an FX settlement it is a realized
    # forex gain/loss, otherwise an unrecorded bank admin fee (cash received <
    # amount settled). Capped so a large discrepancy still surfaces as an error.
    total_debit = sum((to_decimal(r.get("debit")) for r in rows), Decimal("0"))
    total_credit = sum((to_decimal(r.get("credit")) for r in rows), Decimal("0"))
    residual = total_credit - total_debit
    if residual != 0 and abs(residual) <= BANK_CHARGE_MAX_ABS:
        debit = residual if residual > 0 else Decimal("0")
        credit = -residual if residual < 0 else Decimal("0")
        if fx:
            code, name = forex_account(is_debit=debit > 0)
            source_line_id = "forex_diff"
        else:
            code, name = BANK_CHARGE_ACCOUNT_CODE, BANK_CHARGE_ACCOUNT_NAME
            source_line_id = "bank_charge"
        rows.append({
            **base,
            "note": name,
            "debit": debit,
            "credit": credit,
            "amount": compute_realization(code, debit, credit),
            "account_code": code,
            "coa_name": name,
            "source_line_id": source_line_id,
        })

    reconcile_rows(rows, source_id=str(data["id"]))

    return rows


# ============================================================
# OPTIONAL WRAPPER FOR SOURCE RECORD
# ============================================================

def transform_receivable_payment_source_record(source_record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    source_record example:
    {
        "callback_id": 12345,
        "body": "...json string...",
        "created_at": "2026-03-01 10:00:00"
    }
    """
    rows = transform_receivable_payment_from_body(source_record["body"])

    for row in rows:
        row["raw_callback_id"] = to_str(source_record.get("callback_id"))
        row["raw_created_at"] = source_record.get("created_at")

    return rows