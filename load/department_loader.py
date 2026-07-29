"""Load the department master into ``accounting.department``.

The listing is a two-column export — ``DEPARTMENT NO`` and ``DEPARTMENT NAME`` —
saved with an ``.xls`` extension but written as tab-separated text, so it is read
as TSV with a spreadsheet fallback rather than trusting the extension.

    python -m load.department_loader --file transforms/Dept.xls
    python -m load.department_loader --file transforms/Dept.xls --dry-run

The load is a full replace of the master. Codes already used in the ledger but
absent from the listing are reported, because a master that cannot name every
code in use will silently drop rows from any join that keys on it. They are a
warning by default — the listing is the authority on what a department *is* —
and a hard failure under ``--strict``.
"""
from __future__ import annotations

import argparse
import logging
from typing import Any, Dict, List

import pandas as pd
from sqlalchemy import bindparam, text
from sqlalchemy.orm import sessionmaker

from cost_distribution.periods import now_jakarta
from models.finance import Department

logger = logging.getLogger("department_loader")

COLUMNS = {"DEPARTMENT NO": "dept_code", "DEPARTMENT NAME": "dept_name"}

# Tables whose dept_code must be explained by the master.
_LEDGER_TABLES = ["gl", "manual_journal", "sales_invoice", "sales_return",
                  "receivable_payment", "purchase_invoice", "purchase_return",
                  "payable_payment", "cash_in", "cash_out"]


def read_listing(path: str) -> pd.DataFrame:
    """Read the department listing, whatever the extension claims it is."""
    try:
        df = pd.read_csv(path, sep="\t", dtype=str)
        if not set(COLUMNS).issubset({str(c).strip() for c in df.columns}):
            raise ValueError("not the expected tab-separated shape")
    except Exception:
        df = pd.read_excel(path, dtype=str)

    df.columns = [str(c).strip() for c in df.columns]
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing column(s) {sorted(missing)}; "
                         f"found {list(df.columns)}")

    df = df[list(COLUMNS)].rename(columns=COLUMNS)
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()
    df = df[(df["dept_code"] != "") & (df["dept_code"].str.lower() != "nan")]

    for col, label in (("dept_code", "code"), ("dept_name", "name")):
        dupes = df[df[col].duplicated(keep=False)]
        if len(dupes):
            raise ValueError(f"{path}: duplicate department {label}(s): "
                             f"{sorted(set(dupes[col]))}")
    return df.reset_index(drop=True)


def unexplained_codes(engine, codes: set) -> Dict[str, List[str]]:
    """Department codes present in the ledger but absent from the listing."""
    out: Dict[str, List[str]] = {}
    with engine.connect() as conn:
        for table in _LEDGER_TABLES:
            rows = conn.execute(text(
                f"SELECT DISTINCT dept_code FROM {table} WHERE dept_code IS NOT NULL"
            )).scalars().all()
            missing = sorted(c for c in rows if c not in codes)
            if missing:
                out[table] = missing
    return out


def load_departments(path: str, engine, dry_run: bool = False,
                     strict: bool = False, prune: bool = False) -> Dict[str, Any]:
    df = read_listing(path)
    codes = set(df["dept_code"])
    orphans = unexplained_codes(engine, codes)

    report = {"rows": len(df), "orphans": orphans}
    if orphans:
        detail = "; ".join(f"{t}: {', '.join(v)}" for t, v in orphans.items())
        message = (f"the listing does not explain every department code already in "
                   f"the ledger — {detail}. Rows carrying those codes will not "
                   f"resolve against the master.")
        if strict:
            raise ValueError(message + " Add the code(s) to the listing, or correct "
                                       "the rows that use them, then re-run.")
        logger.warning(message)
    if dry_run:
        return report

    # Upsert rather than replace. Departments get added straight to the table
    # when one appears in Zahir before the listing catches up; a blind DELETE
    # would drop those without a trace. Codes only in the table are reported,
    # and removed only when explicitly asked for.
    with engine.connect() as conn:
        existing = set(conn.execute(text("SELECT dept_code FROM department")).scalars())
    surplus = sorted(existing - codes)
    report["surplus"] = surplus
    if surplus and not prune:
        logger.warning("%d department(s) present in the table but not in the listing, "
                       "left in place: %s. Pass --prune to remove them.",
                       len(surplus), ", ".join(surplus))

    now = now_jakarta()
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        if prune and surplus:
            session.execute(
                text("DELETE FROM department WHERE dept_code IN :codes")
                .bindparams(bindparam("codes", expanding=True)), {"codes": surplus})
        for r in df.itertuples():
            row = {"dept_code": r.dept_code, "dept_name": r.dept_name, "updated_at": now}
            if r.dept_code not in existing:
                row["created_at"] = now      # only a new department is "created" now
            session.merge(Department(**row))
        session.commit()
    return report


# (table, code column, name column) for every place a department name is stored.
_NAME_HOLDERS = [(t, "dept_code", "department") for t in _LEDGER_TABLES] + [
    ("sales_detail", "dept_code", "dept"),
]


def canonicalise_names(engine, dry_run: bool = False) -> Dict[str, int]:
    """Rewrite stored department names to the master's, keyed on ``dept_code``.

    The code is the reference; the name is a label that drifts. Rows are matched
    on the code alone, so a row still carrying "CERTIFICATION SERVICES" is
    brought to whatever that code is called today. Rows without a code are left
    untouched — there is nothing to key on, and guessing from the old name is
    what this is meant to stop.
    """
    report: Dict[str, int] = {}
    for table, code_col, name_col in _NAME_HOLDERS:
        with engine.connect() as conn:
            stale = conn.execute(text(f"""
                SELECT COUNT(*) FROM {table} t JOIN department d
                    ON d.dept_code = t.{code_col}
                 WHERE t.{name_col} IS NULL OR t.{name_col} <> d.dept_name
            """)).scalar()
        report[table] = int(stale or 0)
        if stale and not dry_run:
            with engine.begin() as conn:
                conn.execute(text(f"""
                    UPDATE {table} t JOIN department d ON d.dept_code = t.{code_col}
                       SET t.{name_col} = d.dept_name
                     WHERE t.{name_col} IS NULL OR t.{name_col} <> d.dept_name
                """))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the department master")
    parser.add_argument("--file", default="transforms/Dept.xls")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true",
                        help="fail if the ledger uses a code the listing omits")
    parser.add_argument("--prune", action="store_true",
                        help="also delete departments the listing no longer contains")
    parser.add_argument("--canonicalise", action="store_true",
                        help="rewrite stored department names to the master's, "
                             "matching on dept_code")
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
    from load.cost_distribution_db import create_all  # noqa: F401  (cost tables)
    Department.__table__.create(engine, checkfirst=True)

    report = load_departments(args.file, engine, dry_run=args.dry_run,
                              strict=args.strict, prune=args.prune)
    logger.info("%s %d department(s) from %s",
                "would load" if args.dry_run else "loaded", report["rows"], args.file)

    if args.canonicalise:
        renamed = canonicalise_names(engine, dry_run=args.dry_run)
        total = sum(renamed.values())
        for table, n in renamed.items():
            if n:
                logger.info("  %-20s %6d name(s) %s", table, n,
                            "to rewrite" if args.dry_run else "rewritten")
        logger.info("%s %d department name(s) from the master",
                    "would rewrite" if args.dry_run else "rewrote", total)


if __name__ == "__main__":
    main()
