from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, List

from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session


def chunked(items: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    """
    Yield rows in chunks for safer bulk upsert.
    """
    if size <= 0:
        raise ValueError("chunk size must be > 0")

    for i in range(0, len(items), size):
        yield items[i:i + size]


def normalize_row_by_columns(row: Dict[str, Any], allowed_columns: List[str]) -> Dict[str, Any]:
    """
    Keep only allowed target columns.
    """
    normalized = {}
    for col in allowed_columns:
        normalized[col] = row.get(col)
    return normalized


def ensure_decimal_defaults(row: Dict[str, Any], decimal_columns: List[str]) -> Dict[str, Any]:
    """
    Fill missing decimal-like fields with Decimal('0') if absent.
    """
    for col in decimal_columns:
        if row.get(col) is None:
            row[col] = Decimal("0")
    return row


def validate_required_fields(row: Dict[str, Any], required_fields: List[str], context: str = "") -> None:
    """
    Validate minimum required fields before DB load.
    """
    missing = [field for field in required_fields if row.get(field) in (None, "")]
    if missing:
        prefix = f"{context}: " if context else ""
        raise ValueError(f"{prefix}missing required fields: {', '.join(missing)}")


def build_upsert_statement(model, rows: List[Dict[str, Any]], update_columns: List[str]):
    """
    Build MySQL INSERT ... ON DUPLICATE KEY UPDATE statement.
    """
    stmt = insert(model).values(rows)

    update_map = {
        col: getattr(stmt.inserted, col)
        for col in update_columns
    }

    return stmt.on_duplicate_key_update(**update_map)


def upsert_rows(
    session: Session,
    model,
    rows: List[Dict[str, Any]],
    allowed_columns: List[str],
    required_fields: List[str],
    update_columns: List[str],
    decimal_columns: List[str] | None = None,
    chunk_size: int = 500,
    auto_commit: bool = False,
    context: str = "",
) -> Dict[str, int]:
    """
    Generic bulk upsert for line-level finance tables.
    """
    if not rows:
        return {
            "input_rows": 0,
            "processed_rows": 0,
            "chunks": 0,
        }

    decimal_columns = decimal_columns or []

    prepared_rows: List[Dict[str, Any]] = []
    for row in rows:
        db_row = normalize_row_by_columns(row, allowed_columns)
        db_row = ensure_decimal_defaults(db_row, decimal_columns)
        validate_required_fields(db_row, required_fields, context=context)
        prepared_rows.append(db_row)

    total_chunks = 0
    total_processed = 0

    for batch in chunked(prepared_rows, chunk_size):
        stmt = build_upsert_statement(model, batch, update_columns)
        session.execute(stmt)
        total_chunks += 1
        total_processed += len(batch)

    if auto_commit:
        session.commit()

    return {
        "input_rows": len(rows),
        "processed_rows": total_processed,
        "chunks": total_chunks,
    }


def load_module_rows(
    session: Session,
    model,
    rows: List[Dict[str, Any]],
    allowed_columns: List[str],
    required_fields: List[str],
    update_columns: List[str],
    decimal_columns: List[str] | None = None,
    chunk_size: int = 500,
    context: str = "",
) -> Dict[str, int]:
    """
    Safe wrapper with transaction handling.
    """
    try:
        result = upsert_rows(
            session=session,
            model=model,
            rows=rows,
            allowed_columns=allowed_columns,
            required_fields=required_fields,
            update_columns=update_columns,
            decimal_columns=decimal_columns,
            chunk_size=chunk_size,
            auto_commit=False,
            context=context,
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise


def process_source_records(
    session: Session,
    source_records: List[Dict[str, Any]],
    transform_func,
    model,
    allowed_columns: List[str],
    required_fields: List[str],
    update_columns: List[str],
    decimal_columns: List[str] | None = None,
    chunk_size: int = 500,
    context: str = "",
) -> Dict[str, Any]:
    """
    Generic process:
    source records -> transform -> bulk upsert
    """
    all_rows: List[Dict[str, Any]] = []
    failed_records: List[Dict[str, Any]] = []

    for rec in source_records:
        try:
            transformed_rows = transform_func(rec)
            all_rows.extend(transformed_rows)
        except Exception as e:
            failed_records.append({
                "callback_id": rec.get("callback_id"),
                "error": str(e),
            })

    result = {
        "input_records": len(source_records),
        "success_records": len(source_records) - len(failed_records),
        "failed_records": len(failed_records),
        "failed_detail": failed_records,
        "input_rows": 0,
        "processed_rows": 0,
        "chunks": 0,
    }

    if all_rows:
        load_result = load_module_rows(
            session=session,
            model=model,
            rows=all_rows,
            allowed_columns=allowed_columns,
            required_fields=required_fields,
            update_columns=update_columns,
            decimal_columns=decimal_columns,
            chunk_size=chunk_size,
            context=context,
        )
        result.update(load_result)

    return result