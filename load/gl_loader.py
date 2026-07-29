from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.orm import Session

from load.module_loader_config import (
    DECIMAL_COLUMNS,
    GL_ALLOWED_COLUMNS,
    GL_DELETE_SCOPE_COLUMNS,
    GL_REQUIRED_FIELDS,
    GL_UPDATE_COLUMNS,
)
from load.sales_base_loader import load_module_rows
from models.finance import GeneralLedger


def prepare_gl_rows(rows: List[Dict[str, Any]], module_name: str) -> List[Dict[str, Any]]:
    """
    Tag each transformed row for the unified ledger.

    - `module`  : the ETL module name (loader key), e.g. "sales_invoice".
    - `ref_key` : reference document for payment allocations so one payment line
                  split across several invoices/purchases stays unique. Empty
                  string for modules without a reference (kept non-null so the
                  gl unique key behaves consistently).
    """
    prepared: List[Dict[str, Any]] = []
    for row in rows:
        gl_row = dict(row)
        gl_row["module"] = module_name
        gl_row["ref_key"] = row.get("sales_ref_no") or row.get("purchase_ref_no") or ""
        prepared.append(gl_row)
    return prepared


def load_gl_rows(
    session: Session,
    rows: List[Dict[str, Any]],
    module_name: str,
    chunk_size: int = 500,
) -> Dict[str, int]:
    """
    Upsert a module's transformed rows into the unified gl table.

    Replace-by-document is scoped by (type, source_id) so re-loading one module's
    document never disturbs another module's rows that happen to share a source_id.
    """
    if not rows:
        return {"input_rows": 0, "processed_rows": 0, "chunks": 0}

    gl_rows = prepare_gl_rows(rows, module_name)

    return load_module_rows(
        session=session,
        model=GeneralLedger,
        rows=gl_rows,
        allowed_columns=GL_ALLOWED_COLUMNS,
        required_fields=GL_REQUIRED_FIELDS,
        update_columns=GL_UPDATE_COLUMNS,
        decimal_columns=DECIMAL_COLUMNS,
        chunk_size=chunk_size,
        context=f"gl:{module_name}",
        delete_scope_columns=GL_DELETE_SCOPE_COLUMNS,
    )
