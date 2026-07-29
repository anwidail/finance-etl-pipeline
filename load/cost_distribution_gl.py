"""GL fact in MySQL — import the monthly cost lines and read them back.

Lets a whole month run fully from the database: seed a period's GL with
``import_gl_from_workbook`` and run the pipeline with ``--gl-from-db``. The GL
has no period column of its own; each row's period is derived from its date
(MMM-YYYY), the same rule the pipeline uses.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Dict

import pandas as pd
from sqlalchemy import delete, text
from sqlalchemy.orm import sessionmaker

from cost_distribution.periods import date_to_period, now_jakarta, period_to_date
from models.cost_distribution import GLEntry

# ORM attr -> workbook GL sheet column. Sheet-column names match what the
# pipeline expects, so Excel -> DB -> DataFrame is lossless.
GL_COLUMNS = {
    "dept_code": "Dept Code",
    "date": "Date",
    "type": "Type",
    "ref_no": "Ref No.",
    "contact": "Contact",
    "description": "Description",
    "note": "Note",
    "dept": "Dept",
    "project": "Project",
    "curr": "Curr",
    "debit": "Debit",
    "credit": "Credit",
    "balance": "Balance",
    "account_code": "Account Code",
    "account_name": "Account Name",
    "note2": "Note2",
}


def _clean(value):
    return None if pd.isna(value) else value


def import_gl_from_workbook(cfg, period: str, engine, chunk_size: int = 1000) -> int:
    """Seed ``gl_entry`` for ``period`` from the workbook GL sheet. Returns row count.

    Only GL lines whose date falls in ``period`` are imported, and that period's
    rows are replaced first (idempotent per month).
    """
    from load.cost_distribution_db import create_all
    create_all(engine)

    df = pd.read_excel(cfg.input_path, sheet_name=cfg.sheet_gl,
                       dtype={"Ref No.": "string", "Account Code": "string"},
                       engine="openpyxl")
    row_period = pd.to_datetime(df["Date"], errors="coerce").apply(date_to_period)
    df = df[row_period == period].copy()
    # Same canonicalisation as the accounting-sourced import, so both feeds land
    # one account code under one account name.
    if "Account Name" in df.columns:
        df["Account Name"] = (df["Account Name"].astype("string").str.strip()
                              .replace(cfg.account_name_aliases))
    if "Dept" in df.columns:
        df["Dept"] = df["Dept"].astype("string").str.strip().replace(cfg.dept_aliases)

    period_date = period_to_date(period)  # gl_entry.period is a DATE
    Session = sessionmaker(bind=engine, future=True)
    now = now_jakarta()
    with Session() as session:
        session.execute(delete(GLEntry).where(GLEntry.period == period_date))
        records = []
        for _, row in df.iterrows():
            rec = {orm: _clean(row[src]) for orm, src in GL_COLUMNS.items() if src in df.columns}
            # date -> python date
            d = pd.to_datetime(rec.get("date"), errors="coerce")
            rec["date"] = None if pd.isna(d) else d.date()
            rec["period"] = period_date
            rec["created_at"] = now
            rec["updated_at"] = now
            records.append(rec)
        for i in range(0, len(records), chunk_size):
            session.bulk_insert_mappings(GLEntry, records[i:i + chunk_size])
        session.commit()
    return len(records)


def import_gl_from_accounting(cfg, period: str, cost_engine, finance_engine,
                              chunk_size: int = 1000) -> Dict[str, object]:
    """Seed ``gl_entry`` for ``period`` straight from ``accounting.gl``.

    The workbook's GL sheet is a point-in-time export and has been observed to
    miss late postings; sourcing the fact from accounting.gl removes that class
    of gap entirely. Only the cost base is taken — see ``Config.gl_account_prefixes``
    — so revenue and non-operating lines never enter the distribution.

    Returns a report of what was kept and what was filtered out, per reason, so
    the exclusion is auditable rather than silent.
    """
    from load.cost_distribution_db import create_all
    create_all(cost_engine)

    period_date = period_to_date(period)
    next_month = (period_date + timedelta(days=32)).replace(day=1)

    df = pd.read_sql(
        text("""
            SELECT g.date, g.type, g.ref_no, g.contact, g.description, g.note,
                   -- The department master, keyed on the code, is the authority on
                   -- the name. Zahir's callbacks still emit pre-rename names (D010
                   -- arrives as "System Certification Services", which is D01's
                   -- current name), so the stored name is resolved away here rather
                   -- than trusted downstream.
                   COALESCE(dm.dept_name, g.department) AS department,
                   g.dept_code, g.project, g.currency, g.debit, g.credit,
                   g.coa_code, g.coa_name, g.reporting, g.status
            FROM gl g
            LEFT JOIN department dm ON dm.dept_code = g.dept_code
            WHERE g.date >= :start AND g.date < :end
        """),
        finance_engine, params={"start": period_date, "end": next_month},
    )
    n_all = len(df)
    if not n_all:
        raise ValueError(f"accounting.gl has no rows dated in {period}.")

    for col in ("debit", "credit"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # Classify every row so nothing is dropped without being counted.
    code = df["coa_code"].astype("string").str.strip()
    keep_rep = df["reporting"] == cfg.gl_reporting_group
    keep_status = df["status"].isin(list(cfg.gl_status_allowed))
    keep_code = code.notna() & code.str.startswith(tuple(cfg.gl_account_prefixes), na=False)
    keep = keep_rep & keep_status & keep_code

    def _sum(mask):
        return round(float((df.loc[mask, "debit"] - df.loc[mask, "credit"]).sum()), 2)

    excluded = {
        "not_profit_and_loss": (int((~keep_rep).sum()), _sum(~keep_rep)),
        "status_not_allowed": (int((keep_rep & ~keep_status).sum()), _sum(keep_rep & ~keep_status)),
        "outside_cost_accounts": (int((keep_rep & keep_status & ~keep_code).sum()),
                                  _sum(keep_rep & keep_status & ~keep_code)),
    }

    df = df[keep].copy()
    # Canonicalise pre-V.05 names. Anything still unknown is left as-is so the
    # pipeline's PC/COA referential checks flag it rather than this hiding it.
    df["department"] = df["department"].astype("string").str.strip()
    renamed = int(df["department"].isin(cfg.dept_aliases).sum())
    df["department"] = df["department"].replace(cfg.dept_aliases)

    df["coa_name"] = df["coa_name"].astype("string").str.strip()
    renamed_acct = int(df["coa_name"].isin(cfg.account_name_aliases).sum())
    df["coa_name"] = df["coa_name"].replace(cfg.account_name_aliases)

    period_date_ = period_date
    Session = sessionmaker(bind=cost_engine, future=True)
    now = now_jakarta()
    records = []
    for _, row in df.iterrows():
        debit, credit = float(row["debit"]), float(row["credit"])
        # pandas hands back a Timestamp; gl_entry.date is a DATE.
        d = pd.to_datetime(row["date"], errors="coerce")
        records.append({
            "date": None if pd.isna(d) else d.date(),
            "type": _clean(row["type"]),
            "ref_no": _clean(row["ref_no"]),
            "contact": _clean(row["contact"]),
            "description": _clean(row["description"]),
            "note": _clean(row["note"]),
            "dept_code": _clean(row["dept_code"]),
            "dept": _clean(row["department"]),
            "project": _clean(row["project"]),
            "curr": _clean(row["currency"]),
            "debit": debit,
            "credit": credit,
            # The workbook's Balance column; the pipeline derives Amount itself.
            "balance": debit - credit,
            "account_code": _clean(row["coa_code"]),
            "account_name": _clean(row["coa_name"]),
            "note2": None,
            "period": period_date_,
            "created_at": now,
            "updated_at": now,
        })

    with Session() as session:
        session.execute(delete(GLEntry).where(GLEntry.period == period_date_))
        for i in range(0, len(records), chunk_size):
            session.bulk_insert_mappings(GLEntry, records[i:i + chunk_size])
        session.commit()

    # Report the two sides separately: a single debit-credit total would net
    # revenue against cost and read as a meaningless figure.
    credit_side = tuple(cfg.credit_balance_prefixes)
    revenue = sum(-r["balance"] for r in records
                  if credit_side and str(r["account_code"] or "").startswith(credit_side))
    cost = sum(r["balance"] for r in records
               if not (credit_side and str(r["account_code"] or "").startswith(credit_side)))
    return {
        "period": period,
        "source_rows": n_all,
        "imported": len(records),
        "cost": round(float(cost), 2),
        "revenue": round(float(revenue), 2),
        "dept_renamed": renamed,
        "account_renamed": renamed_acct,
        "excluded": excluded,
    }


def read_gl_from_db(period: str, engine) -> pd.DataFrame:
    """Return a workbook-shaped GL DataFrame for ``period`` (Decimal cast to float)."""
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        rows = session.query(GLEntry).filter(GLEntry.period == period_to_date(period)).all()
        if not rows:
            raise ValueError(
                f"No GL rows at period {period!r}. Seed them first with "
                f"import_gl_from_workbook / --import-gl."
            )
        data = [
            {src: (float(v) if isinstance(v, Decimal) else v)
             for orm, src in GL_COLUMNS.items() for v in (getattr(r, orm),)}
            for r in rows
        ]
    return pd.DataFrame(data, columns=list(GL_COLUMNS.values()))
