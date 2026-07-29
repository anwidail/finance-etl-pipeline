"""Backfill ``dept_code`` on rows loaded before the column existed.

The code is read back out of the original Zahir callbacks and matched on the
document/line identity the loaders already store, **not** on the department
name. A name is not a key here: "System Certification Services" appears in the
callbacks under both ``D01`` and ``D010``, so resolving by name would silently
stamp the wrong code on every document booked before that rename.

Run it with::

    python -m load.dept_code_backfill --dry-run
    python -m load.dept_code_backfill

Idempotent: only rows whose ``dept_code`` is still NULL are touched, so it can
be re-run after a later load without disturbing anything already correct.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any, Dict, Iterator, Tuple

from sqlalchemy import bindparam, create_engine, text

logger = logging.getLogger("dept_code_backfill")

# The tables carrying TransactionLineMixin.
TABLES = [
    "manual_journal", "sales_invoice", "sales_return", "receivable_payment",
    "purchase_invoice", "purchase_return", "payable_payment",
    "cash_in", "cash_out", "gl",
]


def _unwrap(value: Any, depth: int = 3) -> Dict[str, Any] | None:
    """Callback bodies arrive as JSON, sometimes double-encoded."""
    for _ in range(depth):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return None
        else:
            break
    return value if isinstance(value, dict) else None


def get_source_engine():
    url = os.getenv("SOURCE_DB_URL")
    if url:
        return create_engine(url, future=True)
    return create_engine(
        f"mysql+pymysql://{os.getenv('SOURCE_DB_USER')}:{os.getenv('SOURCE_DB_PASSWORD')}"
        f"@{os.getenv('SOURCE_DB_HOST', 'localhost')}:{os.getenv('SOURCE_DB_PORT', '3306')}"
        f"/{os.getenv('SOURCE_DB_NAME')}", future=True)


def line_codes(source_engine) -> Tuple[Dict[Tuple[str, str], str], Dict[str, str]]:
    """Read department codes out of the callbacks.

    Returns ``(by_line, by_document)``: the first keyed on
    ``(source_id, line id)``, the second on ``source_id`` alone. A line without
    its own department inherits the document's, mirroring what the transforms do
    when they build the row.
    """
    by_line: Dict[Tuple[str, str], str] = {}
    by_doc: Dict[str, str] = {}
    with source_engine.connect() as conn:
        rows = conn.execution_options(stream_results=True).execute(
            text("SELECT body FROM callback_zahir"))
        for (body,) in rows:
            payload = _unwrap(body)
            if not payload:
                continue
            data = _unwrap(payload.get("data")) if payload.get("data") is not None else payload
            if not isinstance(data, dict):
                continue
            source_id = data.get("id")
            if not source_id:
                continue
            head = data.get("department") if isinstance(data.get("department"), dict) else {}
            head_code = (head or {}).get("code")
            if head_code:
                # Keep the document's department *name* too: the fallback is only
                # safe when it describes the same department as the row does.
                by_doc[str(source_id)] = (str(head_code), str((head or {}).get("name") or ""))
            for item in data.get("line_items") or []:
                if not isinstance(item, dict):
                    continue
                dept = item.get("department") if isinstance(item.get("department"), dict) else {}
                code = (dept or {}).get("code") or head_code
                if code and item.get("id"):
                    by_line[(str(source_id), str(item["id"]))] = str(code)
    return by_line, by_doc


def _same_dept(a: str, b: str) -> bool:
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


def resolve_code(source_id, source_line_id, department, by_line, by_doc) -> str | None:
    """Find a row's department code, most specific match first.

    ``source_line_id`` is not always the bare line id: the transforms suffix it
    with the role a line plays (``…_rev``, ``…_disc``, ``…_<tax id>_tax``) so
    that one source line can post several ledger rows without colliding. The
    bare id is therefore the part before the first underscore — line ids are
    UUIDs, which contain hyphens but never underscores.

    The document-level fallback is used **only** when the document names the
    same department as the row. A document header often differs from its lines,
    and stamping the header's code on a line belonging elsewhere would put a
    code and a name on the same row that contradict each other. Leaving it unset
    is the honest outcome.
    """
    sid = str(source_id)
    slid = str(source_line_id) if source_line_id is not None else ""
    if (sid, slid) in by_line:
        return by_line[(sid, slid)]
    base = slid.split("_", 1)[0]
    if base and (sid, base) in by_line:
        return by_line[(sid, base)]
    doc = by_doc.get(sid)
    if doc and _same_dept(doc[1], department):
        return doc[0]
    return None


def backfill(finance_engine, by_line, by_doc, dry_run: bool = False,
             chunk: int = 1000, recheck: bool = False) -> Dict[str, Dict[str, int]]:
    """Stamp dept_code on rows that lack it. Returns per-table counts.

    With ``recheck`` every row is re-resolved, not only the unset ones, and a
    code that no longer holds is cleared. That is how a wrong stamp gets undone.
    """
    report: Dict[str, Dict[str, int]] = {}
    where = "" if recheck else "WHERE dept_code IS NULL"
    for table in TABLES:
        with finance_engine.connect() as conn:
            pending = conn.execute(text(
                f"SELECT id, source_id, source_line_id, department FROM {table} {where}"
            )).all()

        updates, cleared = [], []
        for row_id, sid, slid, dept in pending:
            code = resolve_code(sid, slid, dept, by_line, by_doc)
            if code:
                updates.append((row_id, code))
            elif recheck:
                cleared.append(row_id)
        report[table] = {"missing": len(pending), "matched": len(updates),
                         "cleared": len(cleared)}

        if updates and not dry_run:
            # Group by code so this is a handful of statements, not one per row.
            by_code: Dict[Any, list] = {}
            for row_id, code in updates:
                by_code.setdefault(code, []).append(row_id)
            if cleared:
                by_code[None] = cleared
            with finance_engine.begin() as conn:
                for code, ids in by_code.items():
                    for i in range(0, len(ids), chunk):
                        conn.execute(
                            text(f"UPDATE {table} SET dept_code = :code WHERE id IN :ids")
                            .bindparams(bindparam("ids", expanding=True)),
                            {"code": code, "ids": ids[i:i + chunk]})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill dept_code from Zahir callbacks")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--recheck", action="store_true",
                        help="re-resolve every row, clearing a code that no longer holds")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from load.cost_distribution_db import get_finance_engine
    by_line, by_doc = line_codes(get_source_engine())
    logger.info("read %d line-level and %d document-level department codes "
                "from the callbacks", len(by_line), len(by_doc))

    report = backfill(get_finance_engine(), by_line, by_doc,
                      dry_run=args.dry_run, recheck=args.recheck)
    total_missing = total_matched = 0
    for table, counts in report.items():
        total_missing += counts["missing"]
        total_matched += counts["matched"]
        if counts["missing"]:
            logger.info("  %-20s %6d examined, %6d resolved, %6d cleared",
                        table, counts["missing"], counts["matched"], counts["cleared"])
    logger.info("%s %d of %d row(s)",
                "would fill" if args.dry_run else "filled", total_matched, total_missing)
    if total_missing - total_matched:
        logger.warning("%d row(s) have no matching callback line — their document "
                       "predates the callback archive, or is not callback-fed "
                       "(e.g. module sales_detail)", total_missing - total_matched)


if __name__ == "__main__":
    main()
