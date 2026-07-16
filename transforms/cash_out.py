from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from transforms.timeutil import to_wib


DEFAULT_CASH_ACCOUNT = {
    "account_code": "111999",
    "coa_name": "Cash/Bank Clearing",
}


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
            f"unbalanced cash_out transform for source_id={source_id}: "
            f"total_debit={total_debit}, total_credit={total_credit}, diff={diff}"
        )


def validate_cash_out_payload(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    end_point = payload.get("end_point")
    if end_point != "cash_outs":
        raise ValueError(f"unexpected end_point: {end_point}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("payload.data must be a dict")

    required_fields = {
        "data.id": data.get("id"),
        "data.number": data.get("number"),
        "data.date": data.get("date"),
    }

    missing = [field for field, value in required_fields.items() if value in (None, "")]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")


def normalize_base_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    department = data.get("department") or {}
    project = data.get("project") or {}
    created = data.get("created") or {}
    created_user = created.get("user") or {}

    return {
        "date": data.get("date"),
        "type": "cash_outs",
        "ref_no": data.get("number"),
        "contact": get_nested(data, "contact", "name"),
        "description": data.get("description"),
        "department": department.get("name"),
        "project": project.get("name"),
        "source_id": data.get("id"),
        "status": data.get("status"),
        "created_at": to_wib(created.get("time")),
        "created_by": created_user.get("name"),
    }


def transform_cash_out_from_body(raw_body: Any) -> List[Dict[str, Any]]:
    payload = parse_json_body(raw_body)
    return transform_cash_out(payload)


def transform_cash_out(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Journal pattern (money paid):
    1. Debit each destination account from data.line_items[]
    2. Credit cash / bank from data.cash
    """
    validate_cash_out_payload(payload)

    data = payload["data"]
    base = normalize_base_fields(data)
    rows: List[Dict[str, Any]] = []

    # 1) Debit each line item account (use of funds)
    for line in data.get("line_items", []) or []:
        line_amount = to_decimal(line.get("amount"))
        if line_amount == 0:
            continue

        dept = line.get("department") or data.get("department") or {}
        proj = line.get("project") or data.get("project") or {}
        acc = line.get("account") or {}
        acc_code = to_str(acc.get("code"))

        rows.append({
            **base,
            "note": line.get("note"),
            "department": dept.get("name"),
            "project": proj.get("name"),
            "debit": line_amount,
            "credit": Decimal("0"),
            "amount": compute_realization(acc_code, line_amount, Decimal("0")),
            "account_code": acc_code,
            "coa_name": acc.get("name"),
            "source_line_id": to_str(line.get("id")),
            "currency": get_nested(line, "currency", "code", default=base.get("currency", "IDR")) or "IDR",
            "exchange_rate": to_decimal(line.get("exchange_rate", 1)),
        })

    # 2) Credit cash / bank account (cash decreases)
    cash = data.get("cash", {}) or {}
    cash_acc = cash.get("account", {}) or {}
    cash_amount = to_decimal(cash.get("amount"))
    cash_code = to_str(cash_acc.get("code"))

    if cash_amount != 0:
        rows.append({
            **base,
            "note": None,
            "debit": Decimal("0"),
            "credit": cash_amount,
            "amount": compute_realization(cash_code, Decimal("0"), cash_amount),
            "account_code": cash_code or DEFAULT_CASH_ACCOUNT["account_code"],
            "coa_name": cash_acc.get("name") or DEFAULT_CASH_ACCOUNT["coa_name"],
            "source_line_id": to_str(cash_acc.get("id")) or "cash",
            "currency": get_nested(cash, "currency", "code", default="IDR") or "IDR",
            "exchange_rate": to_decimal(cash.get("exchange_rate", 1)),
        })

    if not rows:
        raise ValueError(f"no rows generated for cash_out source_id={data.get('id')}")

    reconcile_rows(rows, source_id=str(data["id"]))
    return rows


def transform_cash_out_source_record(source_record: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = transform_cash_out_from_body(source_record["body"])

    for row in rows:
        row["raw_callback_id"] = to_str(source_record.get("callback_id"))
        row["raw_created_at"] = source_record.get("created_at")

    return rows
