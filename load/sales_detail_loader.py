"""Import the ``sales_detail`` workbook into accounting, and post its journal.

Sales invoices have no Zahir callback, so this module stands in for the missing
feed: it reads ``sales_detail.xlsx``, stores the invoice lines in
``sales_detail``, and writes the generated journal into ``gl`` under module
``sales_detail`` (see ``transforms.sales_detail`` for the posting rules).

Run it with::

    python -m load.sales_detail_loader --file sales_detail.xlsx
    python -m load.sales_detail_loader --file sales_detail.xlsx --dry-run

Both tables are replaced per document: re-importing a workbook that contains an
invoice already loaded refreshes it rather than duplicating it, so a corrected
file can simply be re-run.

The load fails closed. If any line names a revenue account that is not in
``accounting.coa``, or any invoice's journal does not balance, nothing is
written and the offending rows are reported.
"""
from __future__ import annotations

import argparse
import logging
from decimal import Decimal
from typing import Any, Dict, List, Tuple

import pandas as pd
from sqlalchemy import bindparam, text
from sqlalchemy.orm import sessionmaker

from cost_distribution.periods import now_jakarta
from load.gl_reporting import normalize_account_code
from models.finance import GeneralLedger, SalesDetail
from transforms.sales_detail import (
    AR_DOMESTIC, AR_FOREIGN, MODULE, VAT_ACCOUNT,
    build_journal, journal_is_balanced, unmapped_accounts,
)

logger = logging.getLogger("sales_detail")

# Workbook column -> SalesDetail attribute. Everything the sheet carries is
# stored; `currency`/`exchange_rate` are optional additions the sheet may not
# have yet, and default to the IDR case.
SHEET_COLUMNS = {
    "date": "date", "ref_no": "ref_no", "contact": "contact",
    "dept_code": "dept_code", "dept": "dept",
    "description": "description", "service_code": "service_code",
    "service_name": "service_name", "service": "service", "account": "account",
    "subtotal": "subtotal", "tax": "tax", "total": "total", "status": "status",
    "created_at": "created_at", "period": "period", "income_type": "income_type",
    "classification": "classification", "location": "location",
    "reporting_line": "reporting_line", "new_dept": "new_dept",
    "dept_div": "dept_div", "pc": "pc",
    "currency": "currency", "exchange_rate": "exchange_rate",
}

# Accounts the journal always needs, whatever the invoice says.
_REQUIRED_ACCOUNTS = (AR_DOMESTIC, AR_FOREIGN, VAT_ACCOUNT)


POSTING_TRIGGER = "trg_sales_detail_post_ins"


def triggers_installed(engine) -> bool:
    """True when the database posts the journal itself.

    With the triggers in place, writing ``sales_detail`` *is* the posting step,
    so anything here that also inserted into ``gl`` would double the entry.
    """
    with engine.connect() as conn:
        return bool(conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.TRIGGERS
             WHERE TRIGGER_SCHEMA = DATABASE() AND TRIGGER_NAME = :name
        """), {"name": POSTING_TRIGGER}).scalar())


def _clean(value):
    return None if pd.isna(value) else value


def _as_date(value):
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.date()


def load_coa_resolver(engine) -> Dict[str, Tuple[str, str]]:
    """Map account **name** and dashed **code** -> (gl account_code, coa_name).

    Both keys land in one dict because the journal looks accounts up two ways:
    revenue by the name the workbook uses, A/R and VAT by their fixed codes.
    """
    coa = pd.read_sql(text("SELECT account_code, account_name FROM coa"), engine)
    resolver: Dict[str, Tuple[str, str]] = {}
    for _, r in coa.iterrows():
        code = str(r["account_code"]).strip()
        name = str(r["account_name"]).strip()
        entry = (normalize_account_code(code), name)
        resolver[name] = entry
        resolver[code] = entry

    missing = [c for c in _REQUIRED_ACCOUNTS if c not in resolver]
    if missing:
        raise ValueError(
            f"accounting.coa is missing account(s) the sales journal needs: {missing}"
        )
    return resolver


def read_workbook(path: str, sheet: str = "sales_detail") -> pd.DataFrame:
    """Read the workbook and normalise it to the ``sales_detail`` column names."""
    df = pd.read_excel(path, sheet_name=sheet, dtype={"ref_no": "string",
                                                      "service_code": "string"})
    df.columns = [str(c).strip() for c in df.columns]
    unknown = [c for c in df.columns if c not in SHEET_COLUMNS]
    if unknown:
        logger.warning("ignoring unrecognised column(s): %s", unknown)
    df = df[[c for c in df.columns if c in SHEET_COLUMNS]].copy()

    for col in ("subtotal", "tax", "total"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    # Currency is not in the source sheet yet: absent means an IDR invoice, which
    # is what decides Domestic vs Foreign A/R.
    if "currency" not in df.columns:
        df["currency"] = "IDR"
    if "exchange_rate" not in df.columns:
        df["exchange_rate"] = 1
    df["currency"] = df["currency"].fillna("IDR").astype(str).str.strip().str.upper()
    df["exchange_rate"] = pd.to_numeric(df["exchange_rate"], errors="coerce").fillna(1)

    for col in ("date", "period", "created_at"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def _records(df: pd.DataFrame, resolver: Dict[str, Tuple[str, str]],
             imported_at) -> List[Dict[str, Any]]:
    out = []
    for _, row in df.iterrows():
        rec = {attr: _clean(row[col]) for col, attr in SHEET_COLUMNS.items()
               if col in df.columns}
        rec["date"] = _as_date(rec.get("date"))
        rec["period"] = _as_date(rec.get("period"))
        name = str(rec.get("account") or "").strip()
        rec["account_code"] = resolver[name][0] if name in resolver else None
        rec["imported_at"] = imported_at
        out.append(rec)
    return out


def check(df: pd.DataFrame, resolver: Dict[str, Tuple[str, str]]) -> Dict[str, Any]:
    """Validate the workbook without touching the database.

    Returns a report; ``ok`` is False when the load must not proceed.
    """
    rows = df.to_dict("records")
    unmapped = unmapped_accounts(rows, resolver)
    gl_rows = [] if unmapped else build_journal(rows, resolver)
    unbalanced = journal_is_balanced(gl_rows) if gl_rows else []

    # subtotal + tax should equal total; a mismatch means the sheet's own
    # arithmetic disagrees with itself. The journal uses subtotal and tax (so it
    # always balances), which makes `total` the value to distrust.
    drift = (df["subtotal"] + df["tax"] - df["total"]).abs()
    return {
        "ok": not unmapped and not unbalanced,
        "lines": len(df),
        "invoices": df["ref_no"].nunique(),
        "gl_rows": len(gl_rows),
        "unmapped": sorted({str(r.get("account") or "").strip() for r in unmapped}),
        "unmapped_rows": [(str(r["ref_no"]), str(r.get("account"))) for r in unmapped],
        "unbalanced": unbalanced,
        "total_drift_rows": int((drift > 0.005).sum()),
        "subtotal": float(df["subtotal"].sum()),
        "tax": float(df["tax"].sum()),
        "total": float(df["total"].sum()),
        "gl": gl_rows,
    }


def import_sales_detail(path: str, engine, sheet: str = "sales_detail",
                        chunk_size: int = 500) -> Dict[str, Any]:
    """Import the workbook into ``sales_detail`` and post its journal to ``gl``.

    Nothing is written unless the whole workbook validates.
    """
    resolver = load_coa_resolver(engine)
    df = read_workbook(path, sheet)
    report = check(df, resolver)

    if report["unmapped"]:
        sample = "\n  ".join(f"{ref}: {acct!r}" for ref, acct in report["unmapped_rows"][:10])
        raise ValueError(
            f"{len(report['unmapped_rows'])} line(s) name a revenue account that is "
            f"not in accounting.coa: {report['unmapped']}\n  {sample}\n"
            f"Add the account(s) to coa, or correct the workbook, then re-run."
        )
    if report["unbalanced"]:
        raise ValueError(f"journal does not balance for invoice(s): {report['unbalanced']}")

    gl_rows = report["gl"]
    imported_at = now_jakarta()
    records = _records(df, resolver, imported_at)
    refs = sorted({r["ref_no"] for r in records})
    # When the triggers are installed the database posts the journal as the rows
    # land; posting it here as well would book every invoice twice.
    db_posts = triggers_installed(engine)

    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        # Replace per document, so a re-import refreshes rather than duplicates.
        for i in range(0, len(refs), 500):
            batch = refs[i:i + 500]
            session.execute(
                text("DELETE FROM sales_detail WHERE ref_no IN :refs")
                .bindparams(bindparam("refs", expanding=True)), {"refs": batch})
            session.execute(
                text("DELETE FROM gl WHERE type = :t AND source_id IN :refs")
                .bindparams(bindparam("refs", expanding=True)),
                {"t": MODULE, "refs": batch})

        for i in range(0, len(records), chunk_size):
            session.bulk_insert_mappings(SalesDetail, records[i:i + chunk_size])
        if not db_posts:
            for i in range(0, len(gl_rows), chunk_size):
                session.bulk_insert_mappings(GeneralLedger, gl_rows[i:i + chunk_size])
        session.commit()
    if db_posts:
        logger.info("journal posted by the database triggers (%d row(s) expected)", len(gl_rows))

    # Fill coa_code/reporting the same way every other module does.
    from load.gl_reporting import enrich_gl_reporting_engine
    enriched = enrich_gl_reporting_engine(engine)

    report["imported_lines"] = len(records)
    report["imported_gl_rows"] = len(gl_rows)
    report["enriched_gl_rows"] = enriched
    report.pop("gl", None)
    return report


def read_sales_detail(engine, period: str = None, refs: List[str] = None) -> pd.DataFrame:
    """Read stored ``sales_detail`` lines — the journal's source when rows were
    inserted straight into the table rather than through the workbook import."""
    sql = "SELECT * FROM sales_detail"
    params: Dict[str, Any] = {}
    where = []
    if period:
        from cost_distribution.periods import period_to_date
        where.append("date >= :start AND date < DATE_ADD(:start, INTERVAL 1 MONTH)")
        params["start"] = period_to_date(period)
    if refs:
        where.append("ref_no IN :refs")
        params["refs"] = list(refs)
    stmt = text(sql + (" WHERE " + " AND ".join(where) if where else ""))
    if refs:
        stmt = stmt.bindparams(bindparam("refs", expanding=True))
    return pd.read_sql(stmt, engine, params=params)


def posting_status(engine) -> Dict[str, Any]:
    """Compare ``sales_detail`` against what is posted in ``gl``.

    Reports invoices that have never been posted and invoices whose posted
    receivable no longer matches the lines — the two ways the table and the
    ledger drift apart when rows are edited in SQL.
    """
    # The receivable is identified by its line id, not by sign: a credit note
    # posts a negative debit, which is this ledger's convention for a reversal.
    rows = pd.read_sql(text(f"""
        SELECT s.ref_no,
               ROUND(SUM(s.subtotal) + SUM(s.tax), 2) AS expected,
               ROUND(COALESCE(g.posted, 0), 2)        AS posted,
               g.posted IS NULL                       AS never_posted
        FROM sales_detail s
        LEFT JOIN (SELECT source_id, SUM(debit) AS posted
                     FROM gl WHERE type = '{MODULE}' AND source_line_id LIKE '%#AR'
                    GROUP BY source_id) g ON g.source_id = s.ref_no
        GROUP BY s.ref_no, g.posted
    """), engine)
    unposted = rows[rows["never_posted"].astype(bool)]["ref_no"].tolist()
    stale = rows[(~rows["never_posted"].astype(bool))
                 & ((rows["expected"] - rows["posted"]).abs() > 0.005)]["ref_no"].tolist()
    orphan = pd.read_sql(text(f"""
        SELECT DISTINCT g.source_id FROM gl g
         WHERE g.type = '{MODULE}'
           AND NOT EXISTS (SELECT 1 FROM sales_detail s WHERE s.ref_no = g.source_id)
    """), engine)["source_id"].tolist()
    return {
        "invoices": len(rows),
        "unposted": unposted,
        "stale": stale,
        "orphan": orphan,
        "in_sync": not (unposted or stale or orphan),
    }


def post_from_db(engine, period: str = None, refs: List[str] = None,
                 chunk_size: int = 500) -> Dict[str, Any]:
    """(Re)generate the ``gl`` journal from the stored ``sales_detail`` rows.

    Safe to re-run: each invoice's ledger rows are replaced, never appended, so
    posting after an edit corrects the entry instead of doubling it. Invoices
    that exist in ``gl`` but no longer in ``sales_detail`` are removed.
    """
    resolver = load_coa_resolver(engine)
    df = read_sales_detail(engine, period=period, refs=refs)
    if df.empty:
        raise ValueError("no sales_detail rows match — nothing to post.")

    for col in ("subtotal", "tax", "total"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    report = check(df, resolver)
    if report["unmapped"]:
        sample = "\n  ".join(f"{ref}: {acct!r}" for ref, acct in report["unmapped_rows"][:10])
        raise ValueError(
            f"{len(report['unmapped_rows'])} line(s) name a revenue account that is "
            f"not in accounting.coa: {report['unmapped']}\n  {sample}"
        )
    if report["unbalanced"]:
        raise ValueError(f"journal does not balance for invoice(s): {report['unbalanced']}")

    gl_rows = report["gl"]
    posted_refs = sorted({r["ref_no"] for r in gl_rows})
    dropped = posting_status(engine)["orphan"] if not (period or refs) else []

    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        for i in range(0, len(posted_refs), 500):
            session.execute(
                text("DELETE FROM gl WHERE type = :t AND source_id IN :refs")
                .bindparams(bindparam("refs", expanding=True)),
                {"t": MODULE, "refs": posted_refs[i:i + 500]})
        for i in range(0, len(dropped), 500):
            session.execute(
                text("DELETE FROM gl WHERE type = :t AND source_id IN :refs")
                .bindparams(bindparam("refs", expanding=True)),
                {"t": MODULE, "refs": dropped[i:i + 500]})
        for i in range(0, len(gl_rows), chunk_size):
            session.bulk_insert_mappings(GeneralLedger, gl_rows[i:i + chunk_size])
        session.commit()

    from load.gl_reporting import enrich_gl_reporting_engine
    enriched = enrich_gl_reporting_engine(engine)
    report.pop("gl", None)
    report.update({"posted_invoices": len(posted_refs), "posted_gl_rows": len(gl_rows),
                   "removed_orphans": len(dropped), "enriched_gl_rows": enriched})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Import sales_detail and post its journal")
    parser.add_argument("--file", default="sales_detail.xlsx", help="path to the workbook")
    parser.add_argument("--sheet", default="sales_detail")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and report, writing nothing")
    parser.add_argument("--post", action="store_true",
                        help="(re)post the journal from the stored sales_detail rows "
                             "instead of importing a workbook")
    parser.add_argument("--status", action="store_true",
                        help="report which invoices are unposted or out of date, then exit")
    parser.add_argument("--period", help="limit --post to one month, MMM-YYYY")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from load.cost_distribution_db import get_finance_engine
    engine = get_finance_engine()

    if args.status:
        st = posting_status(engine)
        logger.info("sales_detail: %d invoice(s)", st["invoices"])
        logger.info("  never posted to gl : %d", len(st["unposted"]))
        logger.info("  posted but stale   : %d", len(st["stale"]))
        logger.info("  in gl, not in table: %d", len(st["orphan"]))
        for label in ("unposted", "stale", "orphan"):
            if st[label]:
                logger.info("  %s sample: %s", label, st[label][:10])
        logger.info("status: %s", "IN SYNC" if st["in_sync"] else "NEEDS POSTING (--post)")
        raise SystemExit(0 if st["in_sync"] else 1)

    if args.post:
        if args.dry_run:
            resolver = load_coa_resolver(engine)
            df = read_sales_detail(engine, period=args.period)
            for col in ("subtotal", "tax", "total"):
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
            rep = check(df, resolver)
            logger.info("would post %d gl row(s) for %d invoice(s)", rep["gl_rows"], rep["invoices"])
            if rep["unmapped"]:
                logger.error("revenue account(s) not in coa: %s", rep["unmapped"])
                for ref, acct in rep["unmapped_rows"]:
                    logger.error("   %s  %s", ref, acct)
            logger.info("dry-run: %s", "OK" if rep["ok"] else "WOULD FAIL")
            raise SystemExit(0 if rep["ok"] else 1)
        rep = post_from_db(engine, period=args.period)
        logger.info("posted %d gl row(s) for %d invoice(s); removed %d orphan(s); "
                    "%d gl row(s) re-enriched",
                    rep["posted_gl_rows"], rep["posted_invoices"],
                    rep["removed_orphans"], rep["enriched_gl_rows"])
        if rep["total_drift_rows"]:
            logger.warning("%d line(s) where subtotal+tax != total", rep["total_drift_rows"])
        return

    if args.dry_run:
        resolver = load_coa_resolver(engine)
        rep = check(read_workbook(args.file, args.sheet), resolver)
        logger.info("lines=%d invoices=%d -> gl rows=%d", rep["lines"], rep["invoices"], rep["gl_rows"])
        logger.info("subtotal=%.2f tax=%.2f total=%.2f", rep["subtotal"], rep["tax"], rep["total"])
        if rep["total_drift_rows"]:
            logger.warning("%d line(s) where subtotal+tax != total", rep["total_drift_rows"])
        if rep["unmapped"]:
            logger.error("revenue account(s) not in coa: %s", rep["unmapped"])
            for ref, acct in rep["unmapped_rows"]:
                logger.error("   %s  %s", ref, acct)
        if rep["unbalanced"]:
            logger.error("unbalanced invoice(s): %s", rep["unbalanced"])
        logger.info("dry-run: %s", "OK" if rep["ok"] else "WOULD FAIL")
        raise SystemExit(0 if rep["ok"] else 1)

    rep = import_sales_detail(args.file, engine, args.sheet)
    logger.info("imported %d line(s) across %d invoice(s); %d gl row(s) posted, "
                "%d gl row(s) re-enriched",
                rep["imported_lines"], rep["invoices"], rep["imported_gl_rows"],
                rep["enriched_gl_rows"])
    if rep["total_drift_rows"]:
        logger.warning("%d line(s) where subtotal+tax != total (journal used subtotal+tax)",
                       rep["total_drift_rows"])


if __name__ == "__main__":
    main()
