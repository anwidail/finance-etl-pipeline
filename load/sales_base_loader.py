from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List

from sqlalchemy import delete, tuple_
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session


def _to_decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def amount_in_original_currency(amount: Any, exchange_rate: Any) -> Decimal:
    """The row `amount` expressed in its original (transaction) currency.

    amount is stored in the booking currency (IDR); dividing by the row exchange
    rate recovers the original-currency value (source amount = amount_origin *
    rate, so amount / rate == amount_origin). A rate of 0/None means no
    conversion (the amount is already in its original currency).
    """
    amt = _to_decimal(amount)
    rate = _to_decimal(exchange_rate)
    if rate == 0:
        return amt.quantize(Decimal("0.01"))
    return (amt / rate).quantize(Decimal("0.01"))


def _is_newer(ts, current) -> bool:
    """Return True if ts is strictly newer than current, treating None as oldest."""
    if ts is None:
        return False
    if current is None:
        return True
    return ts > current


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
    delete_scope_columns: List[str] | None = None,
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
    delete_scope_columns = delete_scope_columns or ["source_id"]

    prepared_rows: List[Dict[str, Any]] = []
    for row in rows:
        db_row = normalize_row_by_columns(row, allowed_columns)
        db_row = ensure_decimal_defaults(db_row, decimal_columns)
        validate_required_fields(db_row, required_fields, context=context)
        prepared_rows.append(db_row)

    # Replace-by-document: remove any existing rows for the documents in this
    # batch before re-inserting. Edited invoices arrive as new callbacks whose
    # line/payment ids differ from the previous version, so a plain upsert keyed
    # on (source_id, source_line_id) would leave stale rows behind and double the
    # totals. Deleting up front (once, before the chunk loop so a later chunk's
    # inserts are not wiped) makes each document a clean replace.
    #
    # The scope columns identify a "document". For per-module tables that is just
    # source_id; for the unified gl table it is (type, source_id) so deleting one
    # module's document never wipes another module's rows sharing that source_id.
    scope_keys = set()
    for r in prepared_rows:
        vals = tuple(r.get(col) for col in delete_scope_columns)
        if all(v not in (None, "") for v in vals):
            scope_keys.add(vals)

    if scope_keys:
        if len(delete_scope_columns) == 1:
            col = getattr(model, delete_scope_columns[0])
            session.execute(delete(model).where(col.in_([k[0] for k in scope_keys])))
        else:
            cols = [getattr(model, c) for c in delete_scope_columns]
            session.execute(
                delete(model).where(tuple_(*cols).in_([list(k) for k in scope_keys]))
            )

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
    delete_scope_columns: List[str] | None = None,
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
            delete_scope_columns=delete_scope_columns,
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise


def delete_documents_by_source_id(
    session: Session,
    model,
    source_ids,
    gl_module_name: str | None = None,
) -> int:
    """Remove a document's rows from the module table and the unified ledger.

    Matched strictly on source_id (data.id UUID) — never on voucher number, which
    is not unique. source_id is globally unique per document, so a plain
    source_id filter cannot touch another module's rows.
    """
    ids = [sid for sid in source_ids if sid not in (None, "")]
    if not ids:
        return 0

    try:
        session.execute(delete(model).where(model.source_id.in_(ids)))
        if gl_module_name:
            from models.finance import GeneralLedger

            session.execute(delete(GeneralLedger).where(GeneralLedger.source_id.in_(ids)))
        session.commit()
    except Exception:
        session.rollback()
        raise

    return len(ids)


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
    gl_module_name: str | None = None,
) -> Dict[str, Any]:
    """
    Generic process:
    source records -> transform -> bulk upsert

    When gl_module_name is set, the same transformed rows are also written to the
    unified general ledger (gl) so every module posts to a single ledger.

    Callback `method` is honored per document (matched on source_id / data.id):
    a DELETE whose callback is the latest for a document removes that document's
    rows (and skips re-inserting them); POST/edit callbacks are transformed and
    upserted as usual.
    """
    # Resolve the latest callback state per document (by created_at): its HTTP
    # method and status. A DELETE that supersedes earlier POST/edit callbacks
    # means the document must be removed; a document whose latest status is not
    # "approved" (e.g. draft) must likewise be kept out of the ledger. Matching
    # is on source_id (UUID), not the voucher number.
    latest_state: Dict[Any, Any] = {}
    for rec in source_records:
        sid = rec.get("source_id")
        if not sid:
            continue
        ts = rec.get("created_at")
        method = (rec.get("method") or "POST").upper()
        status = rec.get("status")
        if sid not in latest_state or _is_newer(ts, latest_state[sid][0]):
            latest_state[sid] = (ts, method, status)

    delete_source_ids = {sid for sid, (ts, method, status) in latest_state.items() if method == "DELETE"}
    # Only approved documents post to the ledger. A non-approved latest state
    # (draft) is removed like a delete so a document reverted to draft does not
    # linger. Status is absent only for synthetic/legacy records — default those
    # to approved so real data is never silently dropped.
    non_approved_source_ids = {
        sid
        for sid, (ts, method, status) in latest_state.items()
        if method != "DELETE" and str(status if status is not None else "approved").lower() != "approved"
    }
    # Skip transforming/loading either set; remove both from the ledger below.
    skip_source_ids = delete_source_ids | non_approved_source_ids

    all_rows: List[Dict[str, Any]] = []
    failed_records: List[Dict[str, Any]] = []

    for rec in source_records:
        # Documents whose latest state is DELETE or non-approved must not be
        # re-inserted; their existing rows are removed after the load step below.
        if rec.get("source_id") in skip_source_ids:
            continue
        try:
            transformed_rows = transform_func(rec)
            for row in transformed_rows:
                # amount restated in the row's original transaction currency.
                row["original_currency"] = amount_in_original_currency(
                    row.get("amount"), row.get("exchange_rate")
                )
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
        "deleted_documents": 0,
        "skipped_non_approved": len(non_approved_source_ids),
    }

    # A single extract can contain several callbacks for the same document (the
    # original plus later edits). Keep only the latest version per source_id so
    # the replace-by-document load below does not re-insert superseded rows.
    if all_rows:
        latest_ts: Dict[Any, Any] = {}
        for row in all_rows:
            sid = row.get("source_id")
            ts = row.get("raw_created_at")
            if sid not in latest_ts or _is_newer(ts, latest_ts[sid]):
                latest_ts[sid] = ts
        all_rows = [
            row for row in all_rows
            if latest_ts.get(row.get("source_id")) == row.get("raw_created_at")
        ]

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

        # Dual-write: post the same rows to the unified general ledger.
        if gl_module_name:
            # Local import avoids a circular import (gl_loader imports this module).
            from load.gl_loader import load_gl_rows

            gl_result = load_gl_rows(
                session=session,
                rows=all_rows,
                module_name=gl_module_name,
                chunk_size=chunk_size,
            )
            result["gl_processed_rows"] = gl_result.get("processed_rows", 0)

    # Remove deleted AND non-approved (draft) documents last so removal always
    # wins over any stale rows a prior approved version may have left behind.
    if skip_source_ids:
        result["deleted_documents"] = delete_documents_by_source_id(
            session=session,
            model=model,
            source_ids=skip_source_ids,
            gl_module_name=gl_module_name,
        )

    return result