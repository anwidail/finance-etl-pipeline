from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from transforms.timeutil import to_wib


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
    tolerance: Decimal = Decimal("1"),
) -> None:
    # Manual journal lines arrive already balanced, but the source carries
    # sub-rupiah fractional amounts (e.g. ...5273 vs ...5) that accumulate to a
    # few cents across many lines. A 1.0 tolerance accepts that rounding while
    # still rejecting genuinely unbalanced journals.
    total_debit = sum((to_decimal(r.get("debit")) for r in rows), Decimal("0"))
    total_credit = sum((to_decimal(r.get("credit")) for r in rows), Decimal("0"))
    diff = total_debit - total_credit

    if abs(diff) > tolerance:
        raise ValueError(
            f"unbalanced manual_journal transform for source_id={source_id}: "
            f"total_debit={total_debit}, total_credit={total_credit}, diff={diff}"
        )


def validate_manual_journal_payload(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    end_point = payload.get("end_point")
    if end_point != "manual_journals":
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
        "type": "manual_journals",
        "ref_no": data.get("number"),
        "contact": None,
        "description": data.get("description"),
        "department": department.get("name"),
        "project": project.get("name"),
        "source_id": data.get("id"),
        "status": data.get("status"),
        "currency": "IDR",
        "exchange_rate": Decimal("1"),
        "created_at": to_wib(created.get("time")),
        "created_by": created_user.get("name"),
    }


def transform_manual_journal_from_body(raw_body: Any) -> List[Dict[str, Any]]:
    payload = parse_json_body(raw_body)
    return transform_manual_journal(payload)


def transform_manual_journal(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Journal pattern:
    Each line_item is already a balanced journal line carrying its own account
    and base-currency debit/credit, so the transform maps line items 1:1 to
    output rows. The `debit`/`credit` fields are pre-converted to the base
    currency (`debit_origin`/`credit_origin` hold the transaction-currency
    amounts), so no exchange-rate multiplication is applied here.
    """
    validate_manual_journal_payload(payload)

    data = payload["data"]
    base = normalize_base_fields(data)
    rows: List[Dict[str, Any]] = []

    for line in data.get("line_items", []) or []:
        acc = line.get("account") or {}
        dept = line.get("department") or data.get("department") or {}
        proj = line.get("project") or data.get("project") or {}
        curr = line.get("currency") or {}

        debit = to_decimal(line.get("debit"))
        credit = to_decimal(line.get("credit"))

        if debit == 0 and credit == 0:
            continue

        account_code = to_str(acc.get("code"))

        rows.append({
            **base,
            "department": dept.get("name"),
            "project": proj.get("name"),
            "note": acc.get("name") or "Manual Journal",
            "debit": debit,
            "credit": credit,
            "amount": compute_realization(account_code, debit, credit),
            "account_code": account_code,
            "coa_name": acc.get("name"),
            "source_line_id": to_str(line.get("id")),
            "currency": curr.get("code") or base["currency"],
            "exchange_rate": to_decimal(line.get("exchange_rate", base["exchange_rate"])),
        })

    if not rows:
        raise ValueError(f"no rows generated for manual_journal source_id={data.get('id')}")

    reconcile_rows(rows, source_id=str(data["id"]))
    return rows


def transform_manual_journal_source_record(source_record: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = transform_manual_journal_from_body(source_record["body"])

    for row in rows:
        row["raw_callback_id"] = to_str(source_record.get("callback_id"))
        row["raw_created_at"] = source_record.get("created_at")

    return rows
